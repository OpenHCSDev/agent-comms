"""OpenHCS agent communications — declaration-owned orchestration."""

__version__ = "0.1.0"

from .declarations import (
    GLOBAL_CHANNEL,
    Activity,
    ActivityLog,
    ActivityState,
    AgentRuntimeInfo,
    Message,
    MessageBus,
    MessageType,
    RelationViolationError,
    RuntimeInfoStore,
    SharedLedger,
    Thread,
    ThreadRegistry,
    ThreadStatus,
    UnregisteredThreadError,
    current_thread,
)
from .operations import Comms, DeleteThreadResult, ForkSpec, RenameThreadResult, wire

__all__ = [
    "Thread",
    "AgentRuntimeInfo",
    "Activity",
    "ActivityLog",
    "ActivityState",
    "Message",
    "ThreadRegistry",
    "MessageBus",
    "SharedLedger",
    "ThreadStatus",
    "MessageType",
    "UnregisteredThreadError",
    "RelationViolationError",
    "RuntimeInfoStore",
    "GLOBAL_CHANNEL",
    "current_thread",
    "Comms",
    "DeleteThreadResult",
    "RenameThreadResult",
    "ForkSpec",
    "wire",
]
