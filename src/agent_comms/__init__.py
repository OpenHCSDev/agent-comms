"""OpenHCS agent communications — declaration-owned orchestration."""

__version__ = "0.1.0"

from .declarations import (
    GLOBAL_CHANNEL,
    Message,
    MessageBus,
    MessageType,
    RelationViolationError,
    SharedLedger,
    Thread,
    ThreadRegistry,
    ThreadStatus,
    UnregisteredThreadError,
    current_thread,
)
from .operations import Comms, ForkSpec, wire

__all__ = [
    "Thread",
    "Message",
    "ThreadRegistry",
    "MessageBus",
    "SharedLedger",
    "ThreadStatus",
    "MessageType",
    "UnregisteredThreadError",
    "RelationViolationError",
    "GLOBAL_CHANNEL",
    "current_thread",
    "Comms",
    "ForkSpec",
    "wire",
]
