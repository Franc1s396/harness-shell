from harness_shell_sidecar import __main__


def test_main_returns_the_stdio_service_exit_code(monkeypatch) -> None:
    class FakeService:
        async def run(self) -> int:
            return 23

    monkeypatch.setattr(
        __main__.SidecarService,
        "for_stdio",
        classmethod(lambda cls: FakeService()),
    )

    assert __main__.main() == 23
