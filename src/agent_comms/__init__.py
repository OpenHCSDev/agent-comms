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
    MessagePage,
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
from .operations import (
    Comms,
    DeleteThreadResult,
    ForkSpec,
    RenameThreadResult,
    TranscriptEvent,
    wire,
)
from .tools import context_tool_catalog, invoke_context_tool, invoke_tool, tool_catalog

__all__ = [
    "Thread",
    "AgentRuntimeInfo",
    "Activity",
    "ActivityLog",
    "ActivityState",
    "Message",
    "ThreadRegistry",
    "MessageBus",
    "MessagePage",
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
    "TranscriptEvent",
    "ForkSpec",
    "wire",
    "tool_catalog",
    "context_tool_catalog",
    "invoke_tool",
    "invoke_context_tool",
]
