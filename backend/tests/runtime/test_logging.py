import hashlib
import io
import json
import logging
from contextlib import redirect_stdout

import httpx
import openai
from harness_shell_sidecar.telemetry.logging import (
    JsonLogFormatter,
    configure_stderr_logging,
    extract_exception_fields,
    log_event,
)


def test_structured_logging_emits_complete_json_to_stderr_only() -> None:
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
        "message": "agent_node_started",
        "agent_run_id": "00000000-0000-0000-0000-000000000001",
        "node": "call_model",
        "react_iteration": 2,
    }
    assert record["timestamp"].endswith("Z")
    assert stdout.getvalue() == ""


def test_unstructured_logging_preserves_the_arbitrary_message() -> None:
    marker = "ordinary-python-log-message"
    stderr = io.StringIO()
    configure_stderr_logging(stderr)

    logging.getLogger("third_party").warning(marker)

    encoded = stderr.getvalue()
    record = json.loads(encoded)
    assert record["event"] == "python_log"
    assert record["message"] == marker
    assert marker in encoded


def test_structured_logging_preserves_arbitrary_fields() -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr)

    log_event(
        logging.getLogger("harness_shell_sidecar.test"),
        logging.INFO,
        "model_request_failed",
        request_body={"prompt": "print-the-complete-business-payload"},
    )

    record = json.loads(stderr.getvalue())
    assert record["request_body"] == {
        "prompt": "print-the-complete-business-payload"
    }


def test_formatter_preserves_structured_record_fields() -> None:
    marker = "structured-business-field-marker"
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

    parsed = json.loads(encoded)
    assert parsed["event"] == "safe-looking-event"
    assert parsed["request_body"] == marker


def test_exception_fields_include_complete_exception_text_and_http_body() -> None:
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

    fields = extract_exception_fields(error)
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
    assert "exception-message-secret-marker" in fields["exception_text"]
    assert json.loads(fields["http_response_body"]) == body
    assert fields["response_body_length"] == len(canonical_body)
    assert fields["response_body_sha256"] == hashlib.sha256(
        canonical_body
    ).hexdigest()
    assert "response-body-secret-marker" in encoded
    assert "exception-message-secret-marker" in encoded


def test_httpx_status_error_includes_status_url_and_raw_response_body() -> None:
    response = httpx.Response(
        502,
        request=httpx.Request("GET", "https://provider.example/health"),
        text="upstream-gateway-response-body",
    )

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        fields = extract_exception_fields(error)

    assert fields["http_status"] == 502
    assert fields["http_response_body"] == "upstream-gateway-response-body"
    assert "https://provider.example/health" in fields["exception_text"]


def test_exception_fields_include_stack_metadata_and_complete_cause_chain() -> None:
    nested_message_marker = "nested-exception-message-marker"

    try:
        try:
            raise ValueError(nested_message_marker)
        except ValueError as cause:
            raise RuntimeError("outer-exception-message-marker") from cause
    except RuntimeError as error:
        fields = extract_exception_fields(error)

    frames = fields["stack_frames"]
    assert isinstance(frames, list)
    assert frames
    assert all(set(frame) == {"file", "line", "function"} for frame in frames)
    encoded = json.dumps(fields, sort_keys=True)
    assert nested_message_marker in encoded
    assert "outer-exception-message-marker" in encoded


def test_unstructured_exception_logging_preserves_traceback_and_message() -> None:
    marker = "unstructured-exception-marker"
    record = logging.LogRecord(
        name="third_party",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="arbitrary-message-marker",
        args=(),
        exc_info=None,
    )

    try:
        raise RuntimeError(marker)
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()

    encoded = JsonLogFormatter().format(record)
    parsed = json.loads(encoded)
    assert parsed["message"] == "arbitrary-message-marker"
    assert marker in parsed["exception_text"]
    assert "Traceback (most recent call last)" in parsed["exception_text"]
