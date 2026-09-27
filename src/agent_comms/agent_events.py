"""Nominal, in-process backend events and the reactions shared by consumers.

These values are never decoded from a transport. Pi JSON is parsed by backend;
ACP publication constructs SDK updates directly from these declared fields.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .declarations import ActivityState
from .mro_dispatch import MroDispatch, handles
from .tool_results import ToolDiff

if TYPE_CHECKING:
    from .operations import Comms


class AgentEvent(ABC):
    """Identity and data for one observation made by the backend."""

    @abstractmethod
    def __init__(self) -> None:
        """Concrete dataclass declarations supply their own payload constructor."""


@dataclass(frozen=True)
class InputDisposition(AgentEvent):
    id: str | None


@dataclass(frozen=True)
class InputStarted(InputDisposition):
    pass


@dataclass(frozen=True)
class InputRefused(InputDisposition):
    pass


@dataclass(frozen=True)
class SettingChangeResult(AgentEvent):
    id: str | None
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class ModelChanged(SettingChangeResult):
    pass


@dataclass(frozen=True)
class ThinkingChanged(SettingChangeResult):
    pass


class ActivityEvent(AgentEvent):
    """An observation whose declaration supplies a shared activity reaction."""

    @property
    @abstractmethod
    def activity_state(self) -> ActivityState:
        pass

    @property
    @abstractmethod
    def activity_detail(self) -> str:
        pass


@dataclass(frozen=True)
class ToolEvent(AgentEvent):
    id: str
    name: str


@dataclass(frozen=True)
class ToolStart(ToolEvent, ActivityEvent):
    title: str = ""
    args: dict[str, Any] | None = None

    @property
    def activity_state(self) -> ActivityState:
        return ActivityState.WORKING

    @property
    def activity_detail(self) -> str:
        return self.title


@dataclass(frozen=True)
class ToolProgress(ToolEvent):
    output: str = ""


@dataclass(frozen=True)
class ToolEnd(ToolEvent):
    ok: bool
    output: str = ""
    diff: ToolDiff | None = None


@dataclass(frozen=True)
class CompactionEvent(AgentEvent):
    reason: str = "unknown"

    @property
    @abstractmethod
    def phase(self) -> str:
        """ACP phase of this observation."""

    @property
    def summary(self) -> str | None:
        return None

    @property
    def will_retry(self) -> bool:
        return False


@dataclass(frozen=True)
class CompactionStart(CompactionEvent):
    @property
    def phase(self) -> str:
        return "start"


@dataclass(frozen=True)
class CompactionEnd(CompactionEvent):
    aborted: bool = False
    summary: str | None = None
    context_used: int | None = None
    will_retry: bool = False

    @property
    def phase(self) -> str:
        return "abort" if self.aborted else "end"


@dataclass(frozen=True)
class CompactionProgress(AgentEvent):
    chunk_index: int
    source_bytes_done: int | None = None
    source_bytes_total: int | None = None
    summary_phase: str | None = None


@dataclass(frozen=True)
class Done(AgentEvent):
    text: str
    ok: bool
    reason_code: str | None = None
    diagnostic: dict[str, Any] | None = None


@dataclass(frozen=True)
class StreamSettled(AgentEvent):
    """Native output settled; final statistics and Done may still follow."""


@dataclass(frozen=True)
class Chunk(AgentEvent):
    text: str


@dataclass(frozen=True)
class CommittedProgress(AgentEvent):
    text: str


@dataclass(frozen=True)
class Thinking(AgentEvent):
    text: str


@dataclass(frozen=True)
class Notice(AgentEvent):
    text: str


@dataclass(frozen=True)
class Error(AgentEvent):
    text: str
    reason_code: str | None = None
    command: str | None = None
    id: str | None = None


@dataclass(frozen=True)
class AgentInfo(AgentEvent):
    model: str | None = None
    session_name: str | None = None
    session_file: str | None = None
    context_used: int | None = None
    context_size: int | None = None
    thinking_level: str | None = None


@dataclass(frozen=True)
class ProviderUsage(AgentEvent):
    response_id: str
    usage: dict[str, Any]


@dataclass(frozen=True)
class SteeringInterrupted(AgentEvent):
    pass


@dataclass(frozen=True)
class McpLiveStatus(AgentEvent):
    receipt: dict[str, Any]


@dataclass(frozen=True)
class TurnState(AgentEvent):
    state: str
    reason_code: str
    elapsed_ms: int
    phase: str
    retryable: bool
    replay_safe: bool
    side_effects_possible: bool
    attempt: dict[str, int | None] | None = None


@dataclass(frozen=True)
class TurnSettled(AgentEvent):
    """Subscriber publication of a settled ACP turn, distinct from the stream."""

    turn_id: str


@dataclass(frozen=True)
class NoActiveTurn(AgentEvent):
    """Replay has no active turn; no fabricated internal turn identity."""


class AgentEventConsumer(MroDispatch, ABC):
    """Shared activity and metadata algorithms; owners supply their context."""

    @property
    @abstractmethod
    def comms(self) -> Comms:
        pass

    @property
    @abstractmethod
    def thread_name(self) -> str:
        pass

    @abstractmethod
    def update_activity(self, state: ActivityState, detail: str) -> None:
        pass

    async def before_agent_info(self, event: AgentInfo) -> None:
        pass

    async def after_agent_info(self, event: AgentInfo) -> None:
        pass

    @handles(AgentInfo)
    async def record_agent_info(self, event: AgentInfo) -> None:
        await self.before_agent_info(event)
        self.comms.set_agent_info(
            self.thread_name,
            model=event.model,
            session_name=event.session_name,
            context_used=event.context_used,
            context_size=event.context_size,
        )
        await self.after_agent_info(event)

    @handles(ActivityEvent)
    async def activity(self, event: ActivityEvent) -> None:
        self.update_activity(event.activity_state, event.activity_detail)
