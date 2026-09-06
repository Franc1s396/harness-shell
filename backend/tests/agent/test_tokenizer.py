"""离线编码失败不得转为网络访问。"""
from pathlib import Path
import socket
import pytest
from harness_shell_sidecar.agent.context_models import ContextError


def test_missing_local_encoding_fails(tmp_path: Path) -> None:
    from harness_shell_sidecar.agent.tokenizer import load_local_encoding
    with pytest.raises(ContextError) as error:
        load_local_encoding(tmp_path, "o200k_base")
    assert error.value.error_code == "CONTEXT_TOKENIZER_UNAVAILABLE"


def test_bundled_encoding_needs_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness_shell_sidecar.agent.tokenizer import load_local_encoding, tokenizer_resource_dir
    def reject_network(*args: object, **kwargs: object) -> None:
        """本地编码期间任何借用网络的尝试都失败。"""
        raise AssertionError("unexpected network")
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    encoding = load_local_encoding(tokenizer_resource_dir(), "o200k_base")
    assert encoding.encode("hello world") == [24912, 2375]
    assert encoding.decode(encoding.encode("中文🙂")) == "中文🙂"


def test_corrupt_ranks_fail_without_fallback(tmp_path: Path) -> None:
    from harness_shell_sidecar.agent.tokenizer import load_local_encoding, tokenizer_resource_dir
    source = tokenizer_resource_dir()
    (tmp_path / "o200k_base.json").write_bytes((source / "o200k_base.json").read_bytes())
    (tmp_path / "o200k_base.tiktoken").write_bytes(b"corrupt")
    with pytest.raises(ContextError) as error:
        load_local_encoding(tmp_path, "o200k_base")
    assert error.value.error_code == "CONTEXT_TOKENIZER_UNAVAILABLE"
