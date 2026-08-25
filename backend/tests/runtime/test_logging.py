import io
import json
import logging

from harness_shell_sidecar.__main__ import configure_stderr_logging


def test_stderr_logging_emits_valid_json(monkeypatch) -> None:
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    configure_stderr_logging()

    logging.getLogger("harness_shell_sidecar.test").warning("runtime stopped")

    record = json.loads(stderr.getvalue())
    assert record["level"] == "WARNING"
    assert record["logger"] == "harness_shell_sidecar.test"
    assert record["message"] == "runtime stopped"
    assert record["timestamp"].endswith("Z")

