"""Public M2 connection contracts."""

from .models import (
    ConnectionProfile,
    ConnectionProfileInput,
    HostKeyCandidate,
    HostKeyRecord,
)
from .repository import ConnectionRepository, ConnectionRepositoryError

__all__ = [
    "ConnectionProfile",
    "ConnectionProfileInput",
    "ConnectionRepository",
    "ConnectionRepositoryError",
    "HostKeyCandidate",
    "HostKeyRecord",
]
