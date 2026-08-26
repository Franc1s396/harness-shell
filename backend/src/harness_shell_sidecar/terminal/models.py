"""Strict contracts for interactive SSH PTY sessions."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


PtyCols = Annotated[int, Field(ge=20, le=500)]
PtyRows = Annotated[int, Field(ge=5, le=300)]


class PtySession(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pty_session_id: UUID
    ssh_session_id: UUID
    connection_id: UUID
    cols: PtyCols
    rows: PtyRows
    state: Literal["OPEN", "CLOSED", "FAILED"]
