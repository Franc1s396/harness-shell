from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from harness_shell_sidecar.web.models import ProblemDetails, RuntimeStateResponse


def test_problem_details_and_runtime_responses_are_strict() -> None:
    request_id = uuid4()
    problem = ProblemDetails(
        type="urn:harness-shell:error:runtime-not-ready",
        title="Runtime not ready",
        status=503,
        error_code="RUNTIME_NOT_READY",
        message="Runtime is not ready",
        request_id=request_id,
        details={},
    )

    assert problem.request_id == request_id
    with pytest.raises(ValidationError):
        RuntimeStateResponse(
            request_id=request_id,
            state="READY",
            unexpected=True,
        )
