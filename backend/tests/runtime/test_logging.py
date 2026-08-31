import hashlib
import io
import json
import logging
from contextlib import redirect_stdout

import httpx
import openai
import pytest

from harness_shell_sidecar.telemetry.logging import (
    JsonLogFormatter,
    configure_stderr_logging,
    extract_safe_exception_fields,
    log_event,
)


def test_structured_logging_emits_allowlisted_json_to_stderr_only() -> None:
    stderr = io.StringIO()
    stdout = io.StringIO()
    configure_stderr_logging(stderr)
    logger = logging.getLogger("harness_shell_sidecar.test")

    with redirect_stdout(stdout):
        log_event(
            logger,
            logging.INFO,
            "agent_node_started",
            agent_run_id="00000000-0000-0000-0000-000000000001",
            node="call_model",
            react_iteration=2,
        )

    record = json.loads(stderr.getvalue())
    assert record == {
        "timestamp": record["timestamp"],
        "level": "INFO",
        "component": "python_sidecar",
        "event": "agent_node_started",
        "logger": "harness_shell_sidecar.test",
        "agent_run_id": "00000000-0000-0000-0000-000000000001",
        "node": "call_model",
        "react_iteration": 2,
    }
    assert record["timestamp"].endswith("Z")
    assert stdout.getvalue() == ""


def test_unstructured_logging_omits_the_arbitrary_message() -> None:
    marker = "user-message-marker-must-not-persist"
    stderr = io.StringIO()
    configure_stderr_logging(stderr)

    logging.getLogger("third_party").warning(marker)

    encoded = stderr.getvalue()
    record = json.loads(encoded)
    assert record["event"] == "unstructured_python_log"
    assert marker not in encoded
    assert "message" not in record


def test_structured_logging_rejects_unknown_fields() -> None:
    with pytest.raises(
        ValueError,
        match="^unsupported log field: request_body$",
    ):
        log_event(
            logging.getLogger("harness_shell_sidecar.test"),
            logging.INFO,
            "model_request_failed",
            request_body="must-not-be-accepted",
        )


def test_structured_logging_rejects_non_scalar_business_objects() -> None:
    with pytest.raises(
        TypeError,
        match="^unsupported log field value: model$",
    ):
        log_event(
            logging.getLogger("harness_shell_sidecar.test"),
            logging.INFO,
            "model_request_failed",
            model={"message": "business-object-marker"},
        )


def test_formatter_does_not_trust_spoofed_structured_record_fields() -> None:
    marker = "spoofed-business-field-marker-must-not-persist"
    record = logging.LogRecord(
        name="third_party",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="safe-looking-event",
        args=(),
        exc_info=None,
    )
    record.harness_event = "safe-looking-event"
    record.harness_fields = {"request_body": marker}

    encoded = JsonLogFormatter().format(record)

    assert marker not in encoded
    assert json.loads(encoded)["event"] == "unstructured_python_log"


def test_safe_exception_fields_exclude_message_headers_url_and_body() -> None:
    body = {
        "error": {
            "type": "authentication_error",
            "code": "invalid_api_key",
            "message": "response-body-secret-marker",
        }
    }
    response = httpx.Response(
        401,
        headers={
            "x-request-id": "req-safe-123",
            "authorization": "Bearer secret-header-marker",
        },
        request=httpx.Request(
            "POST",
            "https://provider.example/v1/responses?token=query-secret-marker",
        ),
        json=body,
    )
    error = openai.AuthenticationError(
        "exception-message-secret-marker",
        response=response,
        body=response.json(),
    )

    fields = extract_safe_exception_fields(error)
    encoded = json.dumps(fields, sort_keys=True)
    canonical_body = (
        b'{"error":{"code":"invalid_api_key",'
        b'"message":"response-body-secret-marker",'
        b'"type":"authentication_error"}}'
    )

    assert fields["exception_type"] == "openai.AuthenticationError"
    assert fields["http_status"] == 401
    assert fields["provider_error_type"] == "authentication_error"
    assert fields["provider_error_code"] == "invalid_api_key"
    assert fields["provider_request_id"] == "req-safe-123"
    assert fields["response_body_length"] == len(canonical_body)
    assert fields["response_body_sha256"] == hashlib.sha256(
        canonical_body
    ).hexdigest()
    for marker in (
        "secret-header-marker",
        "query-secret-marker",
        "response-body-secret-marker",
        "exception-message-secret-marker",
    ):
        assert marker not in encoded


def test_safe_exception_fields_include_only_bounded_stack_metadata() -> None:
    nested_message_marker = "nested-exception-message-marker"

    try:
        try:
            raise ValueError(nested_message_marker)
        except ValueError as cause:
            raise RuntimeError("outer-exception-message-marker") from cause
    except RuntimeError as error:
        fields = extract_safe_exception_fields(error)

    frames = fields["stack_frames"]
    assert isinstance(frames, list)
    assert frames
    assert all(set(frame) == {"file", "line", "function"} for frame in frames)
    encoded = json.dumps(fields, sort_keys=True)
    assert nested_message_marker not in encoded
    assert "outer-exception-message-marker" not in encoded


def test_unstructured_exception_logging_omits_traceback_message() -> None:
    marker = "unstructured-exception-marker-must-not-persist"
    record = logging.LogRecord(
        name="third_party",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="arbitrary-message-marker-must-not-persist",
        args=(),
        exc_info=None,
    )

    try:
        raise RuntimeError(marker)
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()

    encoded = JsonLogFormatter().format(record)
    assert marker not in encoded
    assert "arbitrary-message-marker-must-not-persist" not in encoded
    assert "exception" not in json.loads(encoded)
