from collections.abc import Callable
import asyncio
from uuid import uuid4

import pytest

from harness_shell_sidecar.agent.executor import AgentCancelled
from harness_shell_sidecar.agent.api_configs import ApiConfigRepositoryError
from harness_shell_sidecar.agent.conversations import ConversationRepositoryError
from harness_shell_sidecar.agent.model_gateway import ModelGatewayError
from harness_shell_sidecar.agent.service import AgentServiceError
from harness_shell_sidecar.agent.tools import CommandRejected
from harness_shell_sidecar.connections.repository import ConnectionRepositoryError
from harness_shell_sidecar.credentials.cipher import CredentialCipherError
from harness_shell_sidecar.credentials.repository import CredentialRepositoryError
from harness_shell_sidecar.credentials.service import CredentialServiceError
from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.runtime.dispatcher import DispatchError
from harness_shell_sidecar.runtime.models import RuntimeInitializationFailure
from harness_shell_sidecar.terminal.manager import PtyManagerError
from harness_shell_sidecar.runtime.request_context import (
    RequestCancelledError,
    RequestContext,
)
from harness_shell_sidecar.ssh.errors import SshRuntimeError
from harness_shell_sidecar.web.errors import HttpProblem, build_problem
from harness_shell_sidecar.web.lifespan import RuntimeOwnerError


@pytest.mark.parametrize(
    "error_type",
    [
        ModelGatewayError,
        AgentServiceError,
        AgentCancelled,
        CommandRejected,
        PtyManagerError,
        CredentialServiceError,
        CredentialRepositoryError,
        CredentialCipherError,
        ApiConfigRepositoryError,
        ConversationRepositoryError,
        ConnectionRepositoryError,
        DispatchError,
        RuntimeInitializationFailure,
        RuntimeOwnerError,
        ManualSftpError,
    ],
)
def test_code_bearing_exception_requires_a_specific_safe_message(
    error_type: Callable[[str, str], Exception],
) -> None:
    """Expose both the stable code and the reviewed failure reason."""

    try:
        error = error_type("TEST_FAILURE", "the tested boundary rejected its input")
    except TypeError as constructor_error:
        pytest.fail(
            f"{error_type.__name__} does not accept a specific message: "
            f"{constructor_error}"
        )

    assert str(error) != "TEST_FAILURE"
    assert "the tested boundary rejected its input" in str(error)
    assert getattr(error, "error_code") == "TEST_FAILURE"
    assert getattr(error, "safe_message") == (
        "the tested boundary rejected its input"
    )


def test_http_problem_exception_includes_its_safe_problem_message() -> None:
    problem = build_problem(
        request_id=uuid4(),
        status=400,
        error_code="INVALID_REQUEST_PAYLOAD",
        title="Invalid request",
        message="The request payload did not match the endpoint contract",
    )

    error = HttpProblem(problem)

    assert str(error) == (
        "INVALID_REQUEST_PAYLOAD: "
        "The request payload did not match the endpoint contract"
    )


def test_request_cancellation_identifies_its_failure_point() -> None:
    cancelled = asyncio.Event()
    cancelled.set()
    context = RequestContext(request_id=uuid4(), cancelled=cancelled)

    with pytest.raises(RequestCancelledError) as captured:
        context.require_active()

    assert str(captured.value) == "request was cancelled before application execution"


def test_ssh_runtime_error_identifies_node_and_remote_state() -> None:
    error = SshRuntimeError(
        "SSH_CONNECT_FAILED",
        "the target SSH connection failed",
        node="target_connect",
        recoverable=True,
        remote_state="pre_auth",
    )

    assert str(error) == "SSH_CONNECT_FAILED: the target SSH connection failed"
    assert error.safe_message == "the target SSH connection failed"
    assert error.node == "target_connect"
    assert error.remote_state == "pre_auth"
