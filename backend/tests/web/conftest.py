from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from harness_shell_sidecar.web import create_app
from harness_shell_sidecar.runtime.settings import RuntimeSettings


_ANSI_SGR_SEQUENCE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def request_id() -> UUID:
    """返回合法 HTTP 关联标识。"""

    return uuid4()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """运行全新自主初始化 ASGI 应用的 lifespan。"""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime-data").resolve())
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


@pytest.fixture
def autonomous_client(tmp_path: Path) -> Iterator[TestClient]:
    """运行完全由 ASGI lifespan 初始化的 Runtime。"""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime-data").resolve())
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def reserve_then_release_loopback_port() -> int:
    """返回刚释放的 loopback 端口，用于显式绑定测试。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass
class ProcessProbe:
    """拥有 Sidecar 子进程、捕获输出、HTTP 轮询及清理。"""

    #: 运行中的子进程。
    process: subprocess.Popen[str]
    #: 目前观察到的完整 stdout 行。
    stdout_lines: list[str] = field(default_factory=list)
    #: 目前观察到的完整 stderr 行。
    stderr_lines: list[str] = field(default_factory=list)
    #: 新观察到的 stderr 行队列，用于按条件等待。
    _stderr_queue: queue.Queue[str] = field(default_factory=queue.Queue)
    #: 拥有两条子进程管道的读取线程。
    _readers: tuple[threading.Thread, threading.Thread] | None = None

    def start_readers(self) -> None:
        """持续排空两条子进程管道，防止进程死锁。"""

        assert self.process.stdout is not None
        assert self.process.stderr is not None

        def read_stdout() -> None:
            for line in self.process.stdout:
                self.stdout_lines.append(line.rstrip("\r\n"))

        def read_stderr() -> None:
            for line in self.process.stderr:
                value = line.rstrip("\r\n")
                self.stderr_lines.append(value)
                self._stderr_queue.put(value)

        readers = (
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        )
        for reader in readers:
            reader.start()
        self._readers = readers

    def wait_for_stderr_event(
        self,
        event: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, object]:
        """等待指定名称的控制台事件，不任意休眠。"""

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"stderr event {event!r} was not observed: {self.stderr_lines!r}"
                )
            try:
                line = self._stderr_queue.get(timeout=remaining)
            except queue.Empty as error:
                raise AssertionError(
                    f"stderr event {event!r} was not observed: {self.stderr_lines!r}"
                ) from error
            plain_line = _ANSI_SGR_SEQUENCE.sub("", line)
            columns = plain_line.split(" | ", maxsplit=5)
            if len(columns) != 6:
                continue
            message = columns[-1]
            tokens = message.split()
            if not tokens or tokens[0] != event:
                continue
            record: dict[str, object] = {"event": event}
            for token in tokens[1:]:
                key, separator, value = token.partition("=")
                if not separator:
                    continue
                record[key] = int(value) if value.isdecimal() else value
            return record

    def wait_until_http_ready(self, port: int, *, timeout: float = 10.0) -> None:
        """轮询精确 loopback 存活路由，直到响应或子进程退出。"""

        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{port}/v1/health/live"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(
                    f"Sidecar exited early with {self.process.returncode}: "
                    f"{self.stderr_lines!r}"
                )
            try:
                response = httpx.get(
                    url,
                    headers={"X-Request-ID": str(uuid4())},
                    timeout=0.5,
                )
            except httpx.TransportError:
                time.sleep(0.02)
                continue
            if response.status_code == 200:
                return
            raise AssertionError(
                f"liveness returned {response.status_code}: {response.text}"
            )
        raise AssertionError("Sidecar did not become HTTP-ready before the deadline")

    def close(self) -> None:
        """终止拥有的子进程，并完成捕获输出的排空。"""

        if self.process.poll() is None:
            try:
                self.graceful_shutdown(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=5)
        if self._readers is not None:
            for reader in self._readers:
                reader.join(timeout=2)

    def graceful_shutdown(self, *, timeout: float = 5.0) -> int:
        """发送 Uvicorn 处理的平台控制台信号，并等待 lifespan。"""

        if self.process.poll() is None:
            if sys.platform == "win32":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=timeout)


@pytest.fixture
def sidecar_process() -> Iterator[Callable[..., ProcessProbe]]:
    """启动源码 Sidecar，并确保每个进程测试结束后清理。"""

    probes: list[ProcessProbe] = []

    def start(*args: str) -> ProcessProbe:
        environment = os.environ.copy()
        source = str((Path(__file__).parents[2] / "src").resolve())
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source if not existing else os.pathsep.join((source, existing))
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "harness_shell_sidecar", *args],
            cwd=Path(__file__).parents[2],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform == "win32"
                else 0
            ),
        )
        probe = ProcessProbe(process)
        probe.start_readers()
        probes.append(probe)
        return probe

    yield start

    for probe in probes:
        probe.close()
