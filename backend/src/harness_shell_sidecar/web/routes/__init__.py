"""Typed HTTP route groups."""

from .agent import router as agent_router
from .connections import router as connections_router
from .diagnostics import router as diagnostics_router
from .host_keys import router as host_keys_router
from .manual_sftp import router as manual_sftp_router
from .runtime import router as runtime_router
from .ssh import router as ssh_router
from .terminal import router as terminal_router

__all__ = [
    "agent_router",
    "connections_router",
    "diagnostics_router",
    "host_keys_router",
    "manual_sftp_router",
    "runtime_router",
    "ssh_router",
    "terminal_router",
]
