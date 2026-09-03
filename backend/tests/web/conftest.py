from __future__ import annotations

import json
import os
from pathlib import Path
import queue
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


@pytest.fixture
def request_id() -> UUID:
    """Return one valid HTTP correlation identifier."""

    return uuid4()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Run one fresh autonomous schema-v6 ASGI application lifespan."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime-data").resolve())
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


@pytest.fixture
def autonomous_client(tmp_path: Path) -> Iterator[TestClient]:
    """Run one schema-v6 Runtime initialized entirely by ASGI lifespan."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime-data").resolve())
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def reserve_then_release_loopback_port() -> int:
    """Return a recently released loopback port for an explicit bind test."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass
class ProcessProbe:
    """Own one Sidecar child, captured output, HTTP polling, and cleanup."""

    #: Running child process.
    process: subprocess.Popen[str]
    #: Complete stdout lines observed so far.
    stdout_lines: list[str] = field(default_factory=list)
    #: Complete stderr lines observed so far.
    stderr_lines: list[str] = field(default_factory=list)
    #: Queue of newly observed stderr lines for condition-based waits.
    _stderr_queue: queue.Queue[str] = field(default_factory=queue.Queue)
    #: Reader threads that own both child pipes.
    _readers: tuple[threading.Thread, threading.Thread] | None = None

    def start_readers(self) -> None:
        """Drain both child pipes continuously to prevent process deadlock."""

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
        """Wait for one structured stderr event without arbitrary sleeping."""

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
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == event:
                return record

    def wait_until_http_ready(self, port: int, *, timeout: float = 10.0) -> None:
        """Poll the exact loopback liveness route until it answers or child exits."""

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
        """Terminate the owned child and finish draining captured output."""

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
        """Send the platform console signal Uvicorn handles and await lifespan."""

        if self.process.poll() is None:
            if sys.platform == "win32":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=timeout)


@pytest.fixture
def sidecar_process() -> Iterator[Callable[..., ProcessProbe]]:
    """Start source Sidecars and guarantee cleanup after each process test."""

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
