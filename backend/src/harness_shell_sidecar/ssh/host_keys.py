"""Canonical AsyncSSH Host Key capture and comparison."""

from __future__ import annotations

import base64
from uuid import UUID

import asyncssh

from harness_shell_sidecar.connections import HostKeyCandidate, HostKeyRecord


class HostKeyObserved(Exception):
    def __init__(self, candidate: HostKeyCandidate) -> None:
        super().__init__("Host Key observed")
        self.candidate = candidate


class HostKeyMismatch(Exception):
    def __init__(self, candidate: HostKeyCandidate) -> None:
        super().__init__("Host Key mismatch")
        self.candidate = candidate


def candidate_from_key(
    connection_id: UUID,
    host: str,
    port: int,
    key: asyncssh.SSHKey,
) -> HostKeyCandidate:
    exported = key.export_public_key("openssh").rstrip(b"\r\n")
    return HostKeyCandidate(
        connection_id=connection_id,
        host=host,
        port=port,
        key_algorithm=key.get_algorithm(),
        fingerprint_sha256=key.get_fingerprint("sha256"),
        public_key_openssh_b64=base64.b64encode(exported).decode("ascii"),
    )


class InspectHostKeyClient(asyncssh.SSHClient):
    def __init__(self, connection_id: UUID, host: str, port: int) -> None:
        self._connection_id = connection_id
        self._host = host
        self._port = port

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        raise HostKeyObserved(
            candidate_from_key(
                self._connection_id, self._host, self._port, key
            )
        )


class VerifiedHostKeyClient(asyncssh.SSHClient):
    def __init__(
        self,
        connection_id: UUID,
        host: str,
        port: int,
        trusted: HostKeyRecord,
    ) -> None:
        self._connection_id = connection_id
        self._host = host
        self._port = port
        self._trusted = trusted

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        candidate = candidate_from_key(
            self._connection_id, self._host, self._port, key
        )
        if (
            candidate.key_algorithm == self._trusted.key_algorithm
            and candidate.fingerprint_sha256 == self._trusted.fingerprint_sha256
            and candidate.public_key_openssh_b64
            == self._trusted.public_key_openssh_b64
        ):
            return True
        raise HostKeyMismatch(candidate)


def empty_known_hosts() -> asyncssh.SSHKnownHosts:
    """Return a truthy, explicit empty store so AsyncSSH invokes our callback."""

    return asyncssh.import_known_hosts("")
