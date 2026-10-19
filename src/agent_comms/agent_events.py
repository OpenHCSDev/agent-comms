"""Nominal, in-process backend events and the reactions shared by consumers.

These values are never decoded from a transport. Pi JSON is parsed by backend;
ACP publication constructs SDK updates directly from these declared fields.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from .acp_failure import (
    FailureSource, UnrecordedFailureSource, NativeTurnOutcome, ProviderRetryResult,
    UnresolvedNativeTurnOutcome, SuccessfulNativeTurnOutcome, FailedNativeTurnOutcome,
)
from .declared_family import DeclaredFamily
from .input_attempt import InputAttempt
from .mro_dispatch import MroDispatch, handles
from .pi_payloads import McpLiveAcknowledgement, PiDiagnostic, PiUsage
from .pi_counts import PiCount, UnreportedCount
from .pi_vocabulary import CompactionReason, NoThinkingOverride, ThinkingLevelField, UnknownCompactionReason
from .tool_results import ToolDiff
from .tool_declaration import ToolDeclaration
from .child_process import ProcessIdentity

if TYPE_CHECKING:
    from .started_input import StartedInput
    from .transcript_events import SentTranscript
    from .pi_commands import PiCommand
    from .native_turn_context import NativeContextManifestData
    from .comms import Comms
    from .goal_presentation import GoalSnapshot
    from .pi_events import RetryAttemptEvent
    from .turn_phase import TurnPhase
    from .input_disposition import InputDocument
    from .turn_input_source import OriginalTurnInput
    from .runtime_info import AgentRuntimeInfo
    from .input_drain import InputDrain


class AgentEvent(ABC):
    """Identity and data for one observation made by the backend."""

    @abstractmethod
    def __init__(self) -> None:
        """Concrete dataclass declarations supply their own payload constructor."""


@dataclass(frozen=True)
class GoalChanged(AgentEvent):
    """Current goal authority observed after durable registry/history publication.

    Cross-process notification uses the existing registry watcher/poll; the
    authoritative snapshot supplies this event, never a tool's spelling.
    """

    snapshot: GoalSnapshot


@dataclass(frozen=True)
class ContextObserved(AgentEvent):
    """Text-free SDK input observation; no transcript or status publication."""

    context: NativeContextManifestData


class InputStarted(AgentEvent):
    @abstractmethod
    def captured(self, inputs: InputDrain, session_id: str) -> StartedInput: ...


@dataclass(frozen=True)
class OriginalInputStarted(InputStarted):
    id: ClassVar[None] = None

    def captured(self, inputs: InputDrain, session_id: str) -> StartedInput:
        return inputs.state(session_id).require_first_source()


@dataclass(frozen=True)
class FollowingInputStarted(InputStarted):
    id: str

    def captured(self, inputs: InputDrain, session_id: str) -> StartedInput:
        return inputs.state(session_id).following[self.id]


class InputRefused(AgentEvent):
    @abstractmethod
    async def apply_to(self, inputs: InputDrain, session_id: str) -> None: ...


@dataclass(frozen=True)
class FollowingInputRefused(InputRefused):
    id: str

    async def apply_to(self, inputs: InputDrain, session_id: str) -> None:
        await inputs.input_refused(session_id, self.id)


@dataclass(frozen=True)
class InputCleared(InputRefused):
    """The native queue positively confirmed removal of this exact input."""

    id: str
    native_id: str
    text: str

    async def apply_to(self, inputs: InputDrain, session_id: str) -> None:
        await inputs.input_cleared(session_id, self.id, self.native_id, self.text)


@dataclass(frozen=True)
class PromptCancelled(AgentEvent):
    input_state: type[InputAttempt] | None


@dataclass(frozen=True)
class NativePhaseChanged(AgentEvent):
    """Actual native observer phase; never reconstructed from visible chunks."""
    phase: TurnPhase
    native_process: ProcessIdentity


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


@dataclass(frozen=True)
class ToolEvent(AgentEvent):
    id: str
    name: str


@dataclass(frozen=True)
class ToolStart(ToolEvent):
    title: str = ""
    args: object = None
    kind: str = "other"

    @classmethod
    def from_call(cls, call_id: str, name: str, arguments: object) -> ToolStart:
        declaration = ToolDeclaration.for_name(name)
        title = ToolDeclaration.caption(name, ToolDeclaration.record(name, arguments))
        return cls(call_id, name, title, arguments, declaration.acp_kind)


@dataclass(frozen=True)
class ToolProgress(ToolEvent):
    output: str = ""


@dataclass(frozen=True)
class ToolEnd(ToolEvent):
    ok: bool
    output: str = ""
    diff: ToolDiff | None = None
    sent_message: SentTranscript | None = None


@dataclass(frozen=True)
class CompactionEvent(AgentEvent, DeclaredFamily, affix="Event"):
    reason: type[CompactionReason] = UnknownCompactionReason

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
    context_used: PiCount = field(default_factory=UnreportedCount)
    will_retry: bool = False

    @property
    def phase(self) -> str:
        return "abort" if self.aborted else "end"


@dataclass(frozen=True)
class Done(AgentEvent):
    text: str
    ok: bool
    reason_code: str | None = None
    diagnostic: dict[str, Any] | None = None
    outcome: NativeTurnOutcome = field(default_factory=UnresolvedNativeTurnOutcome)
    # Immutable completion snapshot of TurnOutput's already decoded result.
    retry_result: ProviderRetryResult | None = None

    def resolve(
        self, error: Error | None, document: InputDocument, original: OriginalTurnInput,
    ) -> Done:
        outcome = (SuccessfulNativeTurnOutcome() if self.ok else
                   FailedNativeTurnOutcome.resolve(self, error, document, original))
        return replace(self, outcome=outcome)


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
    # Raw native failure evidence is not a public completed outcome. Done owns
    # its resolution; a recovered turn never publishes an interim refusal.
    text: str
    reason_code: str | None = None
    command: PiCommand | None = None
    diagnostics: tuple[PiDiagnostic, ...] = ()
    source: FailureSource = UnrecordedFailureSource()


class RuntimeInfoObservation(AgentEvent):
    """Record one producer observation through the existing runtime-info owner."""

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return current

    async def record_in(self, consumer: AgentEventConsumer) -> None:
        from .coordinator import Coordination

        await Coordination.run_worker(partial(
            consumer.comms.agents.observe_runtime_info, consumer.thread_name, self,
        ))


@dataclass(frozen=True, kw_only=True)
class ContextUsageObservation(RuntimeInfoObservation):
    """Observe usage without claiming a model, identity or context window."""

    context_used: PiCount = field(default_factory=UnreportedCount)

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return replace(super().project_runtime_info(current), context_used=self.context_used)


@dataclass(frozen=True, kw_only=True)
class ContextSizeObservation(RuntimeInfoObservation):
    """Observe the context window without restating unobserved usage."""

    context_size: PiCount = field(default_factory=UnreportedCount)

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return replace(super().project_runtime_info(current), context_size=self.context_size)


@dataclass(frozen=True, kw_only=True)
class ContextInfo(ContextUsageObservation, ContextSizeObservation):
    """A usage snapshot carries both usage and the context window."""


@dataclass(frozen=True, kw_only=True)
class NativeMetadataObservation(RuntimeInfoObservation):
    """Only native state snapshots carry identity/configuration observations."""

    model: str | None = None
    session_name: str | None = None
    session_file: str | None = None
    thinking_level: ThinkingLevelField = NoThinkingOverride

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return replace(super().project_runtime_info(current),
                       model=self.model, session_name=self.session_name)

    async def record_in(self, consumer: AgentEventConsumer) -> None:
        await consumer.before_native_metadata(self)
        await super().record_in(consumer)
        await consumer.after_native_metadata(self)


@dataclass(frozen=True, kw_only=True)
class AgentInfo(NativeMetadataObservation, ContextSizeObservation):
    """Native state observes metadata and context size, never context usage."""


@dataclass(frozen=True)
class ProviderUsage(AgentEvent):
    response_id: str
    usage: PiUsage


@dataclass(frozen=True)
class SteeringInterrupted(AgentEvent):
    pass


@dataclass(frozen=True)
class McpLiveStatus(AgentEvent):
    acknowledgement: McpLiveAcknowledgement


@dataclass(frozen=True)
class TurnState(AgentEvent):
    """Watchdog observation, not an input disposition or replay assessment.

    Retry permission belongs to the original execution's ReplayAssessments.
    The original Pi retry event carries its own attempt data without another
    tuple, session copy or interpretation of that data here.
    """

    state: str
    reason_code: str
    elapsed_ms: int
    phase: TurnPhase
    attempt: RetryAttemptEvent | None = None


@dataclass(frozen=True)
class TurnSettled(AgentEvent):
    """Subscriber publication of a settled ACP turn, distinct from the stream."""

    turn_id: str


class AgentEventConsumer(MroDispatch):
    """Shared activity and metadata algorithms; owners supply their context."""

    def __init__(self, *, comms: Comms, thread_name: str):
        self._comms = comms
        self._thread_name = thread_name

    @property
    def comms(self) -> Comms:
        return self._comms

    @property
    def thread_name(self) -> str:
        return self._thread_name

    async def before_native_metadata(self, event: NativeMetadataObservation) -> None:
        pass

    async def after_native_metadata(self, event: NativeMetadataObservation) -> None:
        pass

    @handles(RuntimeInfoObservation)
    async def record_runtime_info(self, event: RuntimeInfoObservation) -> None:
        await event.record_in(self)
