from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

from harness_shell_sidecar.manual_sftp.models import DownloadChunk, UploadChunkAck
from harness_shell_sidecar.web.errors import HttpProblem
from harness_shell_sidecar.web.routes.manual_sftp import read_exact_binary_body

def request_headers(**overrides: str) -> dict[str, str]:
    """Create strict request headers for one HTTP application operation."""

    headers = {"X-Request-ID": str(uuid4())}
    headers.update(overrides)
    return headers


def runtime_resources(client):
    """Return the autonomous runtime resource graph."""

    return client.app.state.runtime_owner.require_resources()


class _ChunkApplication:
    """Record raw chunks and return deterministic typed binary projections."""

    def __init__(self) -> None:
        self.uploads: list[bytes] = []

    async def upload_chunk(
        self,
        _context,
        operation_id: UUID,
        *,
        sequence: int,
        offset: int,
        chunk: bytes,
    ) -> UploadChunkAck:
        self.uploads.append(chunk)
        return UploadChunkAck(
            operation_id=operation_id,
            sequence=sequence,
            offset=offset,
            accepted_bytes=len(chunk),
        )

    async def download_chunk(
        self,
        _context,
        operation_id: UUID,
        *,
        sequence: int,
        offset: int,
    ) -> DownloadChunk:
        return DownloadChunk(
            operation_id=operation_id,
            sequence=sequence,
            offset=offset,
            data=b"abc",
            next_offset=offset + 3,
            eof=True,
        )


def test_binary_upload_and_download_preserve_raw_bytes_and_identity(
    client,
    tmp_path: Path,
) -> None:
    resources = runtime_resources(client)
    application = _ChunkApplication()
    resources.manual_sftp_application = application
    operation_id = uuid4()

    uploaded = client.put(
        f"/v1/sftp/uploads/{operation_id}/chunks/1",
        headers=request_headers(
            **{
                "Content-Type": "application/octet-stream",
                "X-Chunk-Offset": "0",
            }
        ),
        content=b"abc",
    )
    downloaded = client.get(
        f"/v1/sftp/downloads/{operation_id}/chunks/1?offset=0",
        headers=request_headers(),
    )

    assert uploaded.status_code == 200
    assert uploaded.json() == {
        "request_id": uploaded.headers["x-request-id"],
        "operation_id": str(operation_id),
        "sequence": 1,
        "offset": 0,
        "accepted_bytes": 3,
    }
    assert application.uploads == [b"abc"]
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["x-chunk-sequence"] == "1"
    assert downloaded.headers["x-chunk-offset"] == "0"
    assert downloaded.headers["x-chunk-byte-count"] == "3"
    assert downloaded.headers["x-chunk-eof"] == "true"
    assert downloaded.headers["x-request-id"]
    assert downloaded.content == b"abc"


@pytest.mark.parametrize(
    ("headers", "content", "expected_status", "expected_code"),
    [
        (
            {"Content-Type": "text/plain", "X-Chunk-Offset": "0"},
            b"abc",
            415,
            "SFTP_CONTENT_TYPE_INVALID",
        ),
        (
            {"Content-Type": "application/octet-stream"},
            b"abc",
            400,
            "SFTP_CHUNK_OFFSET_REQUIRED",
        ),
        (
            {"Content-Type": "application/octet-stream", "X-Chunk-Offset": "-1"},
            b"abc",
            422,
            "SFTP_CHUNK_OFFSET_INVALID",
        ),
        (
            {"Content-Type": "application/octet-stream", "X-Chunk-Offset": "0"},
            b"",
            422,
            "SFTP_CHUNK_EMPTY",
        ),
        (
            {"Content-Type": "application/octet-stream", "X-Chunk-Offset": "0"},
            b"x" * 262_145,
            413,
            "SFTP_CHUNK_TOO_LARGE",
        ),
    ],
    ids=[
        "wrong-content-type",
        "missing-offset",
        "invalid-offset",
        "empty-chunk",
        "oversize-chunk",
    ],
)
def test_upload_chunk_rejects_invalid_binary_contract_before_application(
    client,
    tmp_path: Path,
    headers: dict[str, str],
    content: bytes,
    expected_status: int,
    expected_code: str,
) -> None:
    resources = runtime_resources(client)
    application = _ChunkApplication()
    resources.manual_sftp_application = application

    response = client.put(
        f"/v1/sftp/uploads/{uuid4()}/chunks/0",
        headers=request_headers(**headers),
        content=content,
    )

    assert response.status_code == expected_status
    assert response.json()["error_code"] == expected_code
    assert application.uploads == []


def test_download_rejects_request_body_and_invalid_application_identity(
    client,
    tmp_path: Path,
) -> None:
    resources = runtime_resources(client)
    resources.manual_sftp_application = _ChunkApplication()
    operation_id = uuid4()

    unexpected_body = client.request(
        "GET",
        f"/v1/sftp/downloads/{operation_id}/chunks/0?offset=0",
        headers=request_headers(),
        content=b"unexpected",
    )
    assert unexpected_body.status_code == 400
    assert unexpected_body.json()["error_code"] == "UNEXPECTED_REQUEST_BODY"

    class _MismatchedApplication(_ChunkApplication):
        async def download_chunk(self, *args, **kwargs) -> DownloadChunk:
            requested_operation_id = args[1]
            return DownloadChunk(
                operation_id=requested_operation_id,
                sequence=kwargs["sequence"] + 1,
                offset=kwargs["offset"],
                data=b"abc",
                next_offset=kwargs["offset"] + 3,
                eof=False,
            )

    resources.manual_sftp_application = _MismatchedApplication()
    mismatch = client.get(
        f"/v1/sftp/downloads/{operation_id}/chunks/0?offset=0",
        headers=request_headers(),
    )
    assert mismatch.status_code == 502
    assert mismatch.json()["error_code"] == "SIDECAR_RESPONSE_INVALID"
    assert mismatch.content != b"abc"


def test_upload_rejects_invalid_application_receipt_before_success_response(
    client,
    tmp_path: Path,
) -> None:
    resources = runtime_resources(client)
    operation_id = uuid4()

    class _MismatchedApplication(_ChunkApplication):
        async def upload_chunk(self, *args, **kwargs) -> UploadChunkAck:
            requested_operation_id = args[1]
            return UploadChunkAck(
                operation_id=requested_operation_id,
                sequence=kwargs["sequence"],
                offset=kwargs["offset"],
                accepted_bytes=2,
            )

    application = _MismatchedApplication()
    resources.manual_sftp_application = application
    response = client.put(
        f"/v1/sftp/uploads/{operation_id}/chunks/0",
        headers=request_headers(
            **{
                "Content-Type": "application/octet-stream",
                "X-Chunk-Offset": "0",
            }
        ),
        content=b"abc",
    )

    assert response.status_code == 502
    assert response.json()["error_code"] == "SIDECAR_RESPONSE_INVALID"
    assert application.uploads == []


@pytest.mark.parametrize(
    ("raw_headers", "body", "expected_code"),
    [
        (
            [(b"content-type", b"application/octet-stream")],
            b"a",
            "SFTP_CONTENT_LENGTH_REQUIRED",
        ),
        (
            [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", b"1"),
                (b"content-length", b"1"),
            ],
            b"a",
            "SFTP_CONTENT_LENGTH_REQUIRED",
        ),
        (
            [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", b"01"),
            ],
            b"a",
            "SFTP_CONTENT_LENGTH_INVALID",
        ),
        (
            [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", b"2"),
            ],
            b"a",
            "SFTP_CONTENT_LENGTH_MISMATCH",
        ),
    ],
    ids=["missing", "duplicate", "non-canonical", "mismatch"],
)
def test_binary_reader_rejects_noncanonical_content_length(
    raw_headers: list[tuple[bytes, bytes]],
    body: bytes,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        sent = False

        async def receive() -> dict[str, object]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "PUT",
                "path": "/",
                "headers": raw_headers,
            },
            receive,
        )
        with pytest.raises(HttpProblem) as problem:
            await read_exact_binary_body(
                request,
                request_id=uuid4(),
                minimum=1,
                maximum=262_144,
                required_content_type="application/octet-stream",
            )
        assert problem.value.problem.error_code == expected_code

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/sftp/contexts", {"ssh_session_id": str(uuid4())}),
        ("POST", "/v1/sftp/listings", {"ssh_session_id": str(uuid4()), "path": "/tmp"}),
        ("GET", f"/v1/sftp/listings/{uuid4()}/batches/0", None),
        ("DELETE", f"/v1/sftp/listings/{uuid4()}", None),
        ("POST", "/v1/sftp/metadata/lstat", {"ssh_session_id": str(uuid4()), "path": "/tmp/a"}),
        ("POST", "/v1/sftp/metadata/readlink", {"ssh_session_id": str(uuid4()), "path": "/tmp/a"}),
        ("POST", "/v1/sftp/hashes/sha256", {"ssh_session_id": str(uuid4()), "path": "/tmp/a"}),
        ("POST", "/v1/sftp/uploads/preflight", {"ssh_session_id": str(uuid4()), "path": "/tmp/a"}),
        ("POST", "/v1/sftp/uploads", {}),
        ("PUT", f"/v1/sftp/uploads/{uuid4()}/chunks/0", None),
        ("POST", f"/v1/sftp/uploads/{uuid4()}/finish", None),
        ("POST", f"/v1/sftp/uploads/{uuid4()}/abort", None),
        ("POST", "/v1/sftp/downloads", {}),
        ("GET", f"/v1/sftp/downloads/{uuid4()}/chunks/0?offset=0", None),
        ("POST", f"/v1/sftp/downloads/{uuid4()}/finish", None),
        ("POST", f"/v1/sftp/downloads/{uuid4()}/abort", None),
        ("POST", "/v1/sftp/directories", {}),
        ("POST", "/v1/sftp/renames", {}),
        ("POST", "/v1/sftp/removals", {}),
        ("POST", "/v1/sftp/deletions/preflight", {}),
        ("POST", f"/v1/sftp/deletions/{uuid4()}/execute", {}),
        ("GET", "/v1/sftp/recoveries", None),
        ("GET", f"/v1/sftp/recoveries/{uuid4()}", None),
        ("POST", f"/v1/sftp/recoveries/{uuid4()}/actions", {}),
    ],
)
def test_complete_manual_sftp_route_table_exists(
    client,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    kwargs = {"json": body} if body is not None else {}
    if method == "PUT":
        kwargs = {
            "headers": {
                **request_headers(),
                "Content-Type": "application/octet-stream",
                "X-Chunk-Offset": "0",
            },
            "content": b"a",
        }
    else:
        kwargs["headers"] = request_headers()
    response = client.request(method, path, **kwargs)

    assert response.status_code != 405
    if response.status_code == 404:
        assert "error_code" in response.json()
