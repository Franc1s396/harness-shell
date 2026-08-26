"""Opaque SSH session registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


@dataclass(slots=True)
class SshSession:
    ssh_session_id: UUID
    connection_id: UUID
    connection: Any
    jump_connection: Any | None = None
    child_channels: set[Any] = field(default_factory=set)


class SshSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[UUID, SshSession] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def register(
        self, connection_id: UUID, connection: Any, jump_connection: Any | None = None
    ) -> SshSession:
        session = SshSession(
            uuid4(), connection_id, connection, jump_connection
        )
        self._sessions[session.ssh_session_id] = session
        return session

    def get(self, session_id: UUID) -> SshSession | None:
        return self._sessions.get(session_id)

    async def close(self, session_id: UUID) -> SshSession | None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return None
        first_error: BaseException | None = None

        def remember(error: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = error

        channels = list(session.child_channels)
        for channel in channels:
            try:
                close = getattr(channel, "close", None)
                if close is not None:
                    close()
                else:
                    channel.exit()
            except BaseException as exc:
                remember(exc)
        for channel in channels:
            try:
                await channel.wait_closed()
            except BaseException as exc:
                remember(exc)
        session.child_channels.clear()

        try:
            session.connection.close()
        except BaseException as exc:
            remember(exc)
        try:
            await session.connection.wait_closed()
        except BaseException as exc:
            remember(exc)

        if session.jump_connection is not None:
            try:
                session.jump_connection.close()
            except BaseException as exc:
                remember(exc)
            try:
                await session.jump_connection.wait_closed()
            except BaseException as exc:
                remember(exc)
        if first_error is not None:
            raise first_error
        return session

    async def close_all(self) -> None:
        first_error: BaseException | None = None
        for session_id in list(self._sessions):
            try:
                await self.close(session_id)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
