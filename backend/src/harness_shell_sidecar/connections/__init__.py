"""M2 连接的公共契约。"""

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
