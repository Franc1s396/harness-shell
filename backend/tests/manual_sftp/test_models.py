"""Strict public model tests for user-operated manual SFTP."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from harness_shell_sidecar.manual_sftp.models import RecoverySummary, RemoteEntry


def test_remote_entry_rejects_unknown_fields_and_unsafe_size() -> None:
    payload = {
        "name": "data.txt",
        "path": "/home/demo/data.txt",
        "entry_type": "file",
        "size": 12,
        "mode": 0o100644,
        "mtime_ns": "1770000000000000000",
        "link_target": None,
    }

    assert RemoteEntry.model_validate(payload).size == 12
    with pytest.raises(ValidationError):
        RemoteEntry.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError):
        RemoteEntry.model_validate({**payload, "size": 2**53})


@pytest.mark.parametrize("size", [12.0, "12", True])
def test_remote_entry_rejects_coerced_sizes(size: object) -> None:
    with pytest.raises(ValidationError):
        RemoteEntry(
            name="data.txt",
            path="/home/demo/data.txt",
            entry_type="file",
            size=size,
            mode=0o100644,
            mtime_ns=None,
            link_target=None,
        )


def test_recovery_contract_has_no_local_download_kind() -> None:
    schema = RecoverySummary.model_json_schema()
    encoded = json.dumps(schema, sort_keys=True)

    assert "download" + "_part" not in encoded
    assert "open_local" + "_folder" not in encoded
