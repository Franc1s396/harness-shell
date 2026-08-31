from harness_shell_sidecar import __main__


def test_main_returns_the_stdio_service_exit_code(monkeypatch) -> None:
    calls: list[str] = []

    class FakeService:
        """返回固定退出码的最小 SidecarService 替身。"""

        async def run(self) -> int:
            """模拟服务运行完成并返回可断言的退出码。"""

            calls.append("run")
            return 23

    def configure_stderr_logging() -> None:
        """记录共享 stderr logger 在 Sidecar 构造前完成配置。"""

        calls.append("configure")

    def log_event(_logger, level: int, event: str) -> None:
        """记录安全启动事件发生在 logger 配置之后、服务构造之前。"""

        calls.append(f"log:{level}:{event}")

    def for_stdio() -> FakeService:
        """记录 stdio service 只能在日志配置完成后构造。"""

        calls.append("for_stdio")
        return FakeService()

    monkeypatch.setattr(__main__, "configure_stderr_logging", configure_stderr_logging)
    monkeypatch.setattr(__main__, "log_event", log_event)
    monkeypatch.setattr(
        __main__.SidecarService,
        "for_stdio",
        classmethod(lambda cls: for_stdio()),
    )

    assert __main__.main() == 23
    assert calls == [
        "configure",
        "log:20:sidecar_process_started",
        "for_stdio",
        "run",
    ]
