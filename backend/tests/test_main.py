from harness_shell_sidecar import __main__


def test_main_returns_the_stdio_service_exit_code(monkeypatch) -> None:
    class FakeService:
        """返回固定退出码的最小 SidecarService 替身。"""

        async def run(self) -> int:
            """模拟服务运行完成并返回可断言的退出码。"""

            return 23

    monkeypatch.setattr(
        __main__.SidecarService,
        "for_stdio",
        classmethod(lambda cls: FakeService()),
    )

    assert __main__.main() == 23
