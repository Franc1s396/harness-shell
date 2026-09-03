"""Credential resolution boundary between public identities and SSH secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from harness_shell_sidecar.connections import (
    ConnectionProfile,
    ConnectionRepository,
)

from .cipher import zeroize
from .models import CredentialKind
from .repository import CredentialRepositoryError


class _CredentialRepositoryProtocol(Protocol):
    """Describe the kind-checked secret lookup used by the service."""

    def resolve(
        self,
        credential_id: UUID,
        expected_kind: CredentialKind,
    ) -> bytearray:
        """Return one mutable purpose-checked credential buffer."""


class CredentialServiceError(RuntimeError):
    """Expose one stable credential-resolution failure without secret text."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        """Retain only a stable public error code."""

        self.error_code = error_code
        super().__init__(error_code)


@dataclass(slots=True)
class ResolvedSshConnect:
    """Own one version-frozen SSH request and all temporary secret buffers."""

    #: Direct connection identity.
    connection_id: UUID
    #: Direct profile version rechecked after credential resolution.
    profile_version: int
    #: Password bytes for password authentication.
    password: bytearray | None = None
    #: Imported private-key UTF-8 bytes for key authentication.
    private_key: bytearray | None = None
    #: Optional direct private-key passphrase bytes.
    passphrase: bytearray | None = None
    #: Optional single ProxyJump connection identity.
    jump_connection_id: UUID | None = None
    #: Optional ProxyJump profile version.
    jump_profile_version: int | None = None
    #: Optional ProxyJump password bytes.
    jump_password: bytearray | None = None
    #: Optional ProxyJump private-key bytes.
    jump_private_key: bytearray | None = None
    #: Optional ProxyJump private-key passphrase bytes.
    jump_passphrase: bytearray | None = None
    #: Every allocated secret buffer, including aliases above.
    _allocated: list[bytearray] = field(default_factory=list, repr=False)

    def close(self) -> None:
        """Overwrite every temporary secret buffer; repeated calls are safe."""

        for secret in self._allocated:
            zeroize(secret)
        self._allocated.clear()


class CredentialService:
    """Snapshot profiles, resolve exact credential kinds, and reject races."""

    _connections: ConnectionRepository
    _credentials: _CredentialRepositoryProtocol

    def __init__(
        self,
        connections: ConnectionRepository,
        credentials: _CredentialRepositoryProtocol,
    ) -> None:
        """Bind Runtime-owned repositories without taking their lifecycle."""

        self._connections = connections
        self._credentials = credentials

    def build_ssh_connect(self, connection_id: UUID) -> ResolvedSshConnect:
        """Resolve direct and one-hop credentials against stable profile versions."""

        direct = self._required_profile(connection_id)
        jump = (
            None
            if direct.proxy_jump_id is None
            else self._required_profile(direct.proxy_jump_id)
        )
        if jump is not None and jump.proxy_jump_id is not None:
            raise CredentialServiceError("MULTI_HOP_PROXY_FORBIDDEN")

        resolved = ResolvedSshConnect(
            connection_id=direct.connection_id,
            profile_version=direct.version,
            jump_connection_id=None if jump is None else jump.connection_id,
            jump_profile_version=None if jump is None else jump.version,
        )
        try:
            (
                resolved.password,
                resolved.private_key,
                resolved.passphrase,
            ) = self._resolve_profile(direct, resolved._allocated)
            if jump is not None:
                (
                    resolved.jump_password,
                    resolved.jump_private_key,
                    resolved.jump_passphrase,
                ) = self._resolve_profile(jump, resolved._allocated)
            self._require_same_version(direct)
            if jump is not None:
                self._require_same_version(jump)
            return resolved
        except CredentialServiceError:
            resolved.close()
            raise
        except CredentialRepositoryError as error:
            resolved.close()
            raise CredentialServiceError(error.error_code) from None
        except BaseException:
            resolved.close()
            raise

    def _required_profile(self, connection_id: UUID) -> ConnectionProfile:
        """Load one required profile or return a stable not-found error."""

        profile = self._connections.get(connection_id)
        if profile is None:
            raise CredentialServiceError("CONNECTION_NOT_FOUND")
        return profile

    def _resolve_profile(
        self,
        profile: ConnectionProfile,
        allocated: list[bytearray],
    ) -> tuple[bytearray | None, bytearray | None, bytearray | None]:
        """Resolve exactly the credential kinds declared by one profile."""

        if profile.auth_kind == "password":
            password = self._credentials.resolve(
                profile.credential_id,
                "ssh_password",
            )
            allocated.append(password)
            return password, None, None

        private_key = self._credentials.resolve(
            profile.credential_id,
            "imported_private_key",
        )
        allocated.append(private_key)
        passphrase = None
        if profile.passphrase_credential_id is not None:
            passphrase = self._credentials.resolve(
                profile.passphrase_credential_id,
                "private_key_passphrase",
            )
            allocated.append(passphrase)
        return None, private_key, passphrase

    def _require_same_version(self, snapshot: ConnectionProfile) -> None:
        """Reject deletion or any successful update after secret resolution."""

        current = self._connections.get(snapshot.connection_id)
        if current is None or current.version != snapshot.version:
            raise CredentialServiceError("CONNECTION_PROFILE_CHANGED")


__all__ = [
    "CredentialService",
    "CredentialServiceError",
    "ResolvedSshConnect",
]
