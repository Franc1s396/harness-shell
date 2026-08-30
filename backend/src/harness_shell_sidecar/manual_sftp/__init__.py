"""User-operated manual SFTP domain with no Agent-callable surface."""

from .errors import ManualSftpError
from .models import (
    DeletePlanSummary,
    ListingBatch,
    ManualSftpContext,
    OperationTerminalProjection,
    RecoverySummary,
    RemoteEntry,
    RemoteFileHash,
    TransferSnapshot,
)

__all__ = [
    "DeletePlanSummary",
    "ListingBatch",
    "ManualSftpContext",
    "ManualSftpError",
    "OperationTerminalProjection",
    "RecoverySummary",
    "RemoteEntry",
    "RemoteFileHash",
    "TransferSnapshot",
]
