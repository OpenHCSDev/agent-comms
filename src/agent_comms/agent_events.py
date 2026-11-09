from agent_comms.execution.runtime_info_projection import RuntimeInfoProjection

"""Nominal, in-process backend events and the reactions shared by consumers.

These values are never decoded from a transport. Pi JSON is parsed by backend;
ACP publication constructs SDK updates directly from these declared fields.
"""

from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from typing import ClassVar

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.execution.agent_event import AgentEvent as AgentEvent
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.acp_failure import (
    FailureSource,
    NativeTurnOutcome,
    NoRetry,
    ProviderRetryResult,
    SuccessfulNativeTurnOutcome,
    UnrecordedFailureSource,
    UnresolvedNativeTurnOutcome,
)
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_output import NativeOutputLocation, NativeRecordLocation
from agent_comms.execution.pi_vocabulary import (
    CompactionReason,
    NoThinkingOverride,
    ThinkingLevelField,
    UnknownCompactionReason,
)
from agent_comms.execution.pi_wire import (
    McpLiveAcknowledgement,
    PiDiagnostic,
    PiOmitted,
    PiRequestId,
    PiUsage,
)
from agent_comms.execution.runtime_info import AgentRuntimeInfo
from agent_comms.execution.started_input import StartedInput
from agent_comms.execution.tool_results import NoSentMessage, NoToolDiff, ToolDiff
from agent_comms.execution.turn_failure import NoTurnFailure, TurnFailure
from agent_comms.execution.turn_goal_account import OriginGoalSettlement, VerifiedGoalSettlement
from agent_comms.execution.turn_item import UsageChargeId
from agent_comms.execution.turn_terminal import FailedGoalTurn
from agent_comms.input_attempt import InputAttempt
from agent_comms.execution.input_event_source import InputEventSource
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId, InputId, ToolCallId, TurnId
from agent_comms.pi_counts import PiCount, UnreportedCount
from agent_comms.registry.thread_model import ThreadModel, UndeclaredModel
from agent_comms.execution.runtime_info_source import RuntimeInfoSource
from agent_comms.team.rule_deal import CompactedDeal, Redeals
from agent_comms.transcript.transcript_events import SentTranscript
from agent_comms.transport.tool_declaration import OtherAction, ToolAction, ToolDeclaration


@dataclass(frozen=True)
class NativeRoundCommitted(AgentEvent):
    location: NativeRecordLocation


@dataclass(frozen=True)
class NativeOutputCommitted(AgentEvent):
    location: NativeOutputLocation


@dataclass(frozen=True)
class PromptAccepted(AgentEvent):
    """The command's matching successful response acknowledged this turn's prompt."""


class InputStarted(AgentEvent):
    @abstractmethod
    def captured(self, inputs: InputEventSource, session_id: AcpSessionId) -> StartedInput: ...


@dataclass(frozen=True)
class OriginalInputStarted(InputStarted):
    id: ClassVar[None] = None

    def captured(self, inputs: InputEventSource, session_id: AcpSessionId) -> StartedInput:
        return inputs.state(session_id).require_first_source()


@dataclass(frozen=True)
class FollowingInputStarted(InputStarted):
    id: InputId

    def captured(self, inputs: InputEventSource, session_id: AcpSessionId) -> StartedInput:
        return inputs.state(session_id).following[self.id]


class InputRefused(AgentEvent):
    @abstractmethod
    async def apply_to(self, inputs: InputEventSource, session_id: AcpSessionId) -> None: ...


@dataclass(frozen=True)
class FollowingInputRefused(InputRefused):
    id: InputId

    async def apply_to(self, inputs: InputEventSource, session_id: AcpSessionId) -> None:
        await inputs.input_refused(session_id, self.id)


@dataclass(frozen=True)
class InputCleared(InputRefused):
    """The native queue positively confirmed removal of this exact input."""

    id: InputId
    native_id: InputId
    text: str

    async def apply_to(self, inputs: InputEventSource, session_id: AcpSessionId) -> None:
        await inputs.input_cleared(session_id, self.id, self.native_id, self.text)


@dataclass(frozen=True)
class PromptCancelled(AgentEvent):
    input_state: type[InputAttempt] | None


@dataclass(frozen=True)
class SettingChangeResult(AgentEvent):
    id: PiRequestId
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
    id: ToolCallId
    name: str


@dataclass(frozen=True)
class ToolStart(ToolEvent):
    title: str = ""
    args: object = None
    kind: type[ToolAction] = OtherAction

    @classmethod
    def from_call(cls, call_id: ToolCallId, name: str, arguments: object) -> ToolStart:
        declaration = ToolDeclaration.for_name(name)
        title = ToolDeclaration.caption(name, ToolDeclaration.record(name, arguments, call_id=call_id))
        return cls(call_id, name, title, arguments, declaration.declared_action())


@dataclass(frozen=True)
class ToolProgress(ToolEvent):
    output: str = ""


@dataclass(frozen=True)
class ToolEnd(ToolEvent):
    ok: bool
    output: str = ""
    diff: ToolDiff | NoToolDiff = NoToolDiff()
    sent_message: SentTranscript | NoSentMessage = NoSentMessage()


@dataclass(frozen=True)
class CompactionEvent(AgentEvent, DeclaredFamily, affix="Event"):
    reason: type[CompactionReason] = UnknownCompactionReason

    @property
    @abstractmethod
    def phase(self) -> str:
        """ACP phase of this observation."""

    def deal_rules(self, deal: Redeals) -> None:
        """Only a compaction that placed its summary drops the rules from context."""

    @property
    def will_retry(self) -> bool:
        return False


@dataclass(frozen=True)
class CompactionStart(CompactionEvent):
    @property
    def phase(self) -> str:
        return "start"


@dataclass(frozen=True)
class CompactionFinish(CompactionEvent, declared_name=None):
    """A compaction stopped: it placed its summary, or it aborted and left the context alone."""

    context_used: PiCount = field(default_factory=UnreportedCount)
    will_retry: bool = False

    async def transcript_changed(self, publish: Callable[[], Awaitable[None]]) -> None:
        """An aborted compaction left the transcript as it was."""


@dataclass(frozen=True)
class CompactionAbort(CompactionFinish):
    @property
    def phase(self) -> str:
        return "abort"


@dataclass(frozen=True)
class CompactionEnd(CompactionFinish):
    @property
    def phase(self) -> str:
        return "end"

    def deal_rules(self, deal: Redeals) -> None:
        """The summary that replaced the context is a paraphrase of the rules: deal them again,
        saying when it compacted so the fresh context reads the cause instead of guessing it."""
        deal.redeal(CompactedDeal(datetime.now().astimezone().isoformat(timespec="seconds")))

    async def transcript_changed(self, publish: Callable[[], Awaitable[None]]) -> None:
        await publish()


@dataclass(frozen=True)
class Done(AgentEvent, FailedGoalTurn):
    text: str
    ok: bool
    failure: TurnFailure = field(default_factory=NoTurnFailure)
    diagnostic: dict[str, int] | None = None
    outcome: NativeTurnOutcome = field(default_factory=UnresolvedNativeTurnOutcome)
    # Immutable completion snapshot of TurnOutput's already decoded result.
    retry_result: ProviderRetryResult = field(default_factory=NoRetry)

    def require_preparation_success(self) -> None:
        if not self.ok:
            raise NativePiUnavailable(self.text)

    def retain_terminal(self, turn) -> None:
        turn.terminal = self

    def resolve(
        self, error: Error | None,
        resolve_failure: Callable[[Done, Error | None], NativeTurnOutcome],
    ) -> Done:
        outcome = (SuccessfulNativeTurnOutcome() if self.ok else
                   resolve_failure(self, error))
        return replace(self, outcome=outcome)

    @property
    def reason_code(self) -> str | None:
        return None if self.ok else self.turn_failure.declared_name

    def failure_observation(self, account, permit, captured):
        return None if self.ok else super().failure_observation(account, permit, captured)

    def settle_current(self, account) -> None:
        if self.ok:
            VerifiedGoalSettlement.settle_current(account.comms, account.owner, account.turn)

    def settle_launched(self, account, permit, store, captured, current) -> None:
        if self.ok:
            current.settle(VerifiedGoalSettlement, store, permit, account.turn)
        super().settle_launched(account, permit, store, captured, current)

    def settle_origin(self, account, permit, store, goal_id, current) -> None:
        if self.ok:
            permit.settle_goal(OriginGoalSettlement, store, current, account.turn)
        super().settle_origin(account, permit, store, goal_id, current)

    def reported(self, event: Done) -> Done:
        return replace(event, ok=False, text="Duplicate native terminal result")

    def require_reported(self) -> Done:
        return self

    async def prepare_publication(self, progress: object) -> Done:
        return self

    async def report_failure(
        self, progress: object, error: Exception, kind: type[TurnFailure]
    ) -> NativeTurnOutcome:
        return self.outcome


@dataclass(frozen=True)
class StreamSettled(AgentEvent):
    """Native output settled; final statistics and Done may still follow."""


@dataclass(frozen=True)
class TextEvent(AgentEvent):
    """Text keeps its producer-declared meaning across ACP publication."""

    text: str

    def acp_updates[Update](
        self, notice_update: Callable[[], Update],
    ) -> tuple[Update, ...]:
        return ()


@dataclass(frozen=True)
class Chunk(TextEvent):
    """An incremental fragment of the current native assistant message."""


@dataclass(frozen=True)
class CommittedProgress(AgentEvent):
    text: str


@dataclass(frozen=True)
class Thinking(AgentEvent):
    text: str


@dataclass(frozen=True)
class Notice(TextEvent):
    """A standalone message, complete independently of the native turn phase."""

    def acp_updates[Update](
        self, notice_update: Callable[[], Update],
    ) -> tuple[Update, ...]:
        return (notice_update(),)


@dataclass(frozen=True)
class Error(AgentEvent):
    # Raw native failure evidence is not a public completed outcome. Done owns
    # its resolution; a recovered turn never publishes an interim refusal.
    text: str
    reason_code: str | None = None
    diagnostics: tuple[PiDiagnostic, ...] = ()
    source: FailureSource = UnrecordedFailureSource()


class RuntimeInfoObservation(AgentEvent, RuntimeInfoProjection[AgentRuntimeInfo]):
    """Record one producer observation through the existing runtime-info owner."""

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return current

    async def record_in[Client: RuntimeInfoSource[RuntimeInfoObservation]](
        self, consumer: AgentEventConsumer[Client],
    ) -> None:
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
class ModelObservation(RuntimeInfoObservation):
    """Observe the model that answers the thread."""

    model: ThreadModel = UndeclaredModel()

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return replace(super().project_runtime_info(current), model=self.model)


@dataclass(frozen=True, kw_only=True)
class TranscriptModelUsage(ModelObservation, ContextUsageObservation):
    """A CLI transcript's answer names its model and context usage, never the window."""


@dataclass(frozen=True, kw_only=True)
class NativeMetadataObservation(ModelObservation):
    """Only native state snapshots carry identity/configuration observations."""

    session_file: str | None = None
    thinking_level: ThinkingLevelField = NoThinkingOverride

    async def record_in[Client: RuntimeInfoSource[RuntimeInfoObservation]](
        self, consumer: AgentEventConsumer[Client],
    ) -> None:
        await consumer.before_native_metadata(self)
        await super().record_in(consumer)
        await consumer.after_native_metadata(self)


@dataclass(frozen=True, kw_only=True)
class AgentInfo(NativeMetadataObservation, ContextSizeObservation):
    """Native state observes metadata and context size, never context usage."""


@dataclass(frozen=True)
class ProviderUsage(AgentEvent):
    response_id: UsageChargeId
    usage: PiUsage

    @classmethod
    def charges(cls, usage: PiUsage, identity: UsageChargeId | PiOmitted) -> tuple[ProviderUsage, ...]:
        return tuple(cls(response_id=key, usage=usage) for key in usage.charged(identity))


@dataclass(frozen=True)
class SteeringInterrupted(AgentEvent):
    pass


@dataclass(frozen=True)
class McpLiveStatus(AgentEvent):
    acknowledgement: McpLiveAcknowledgement


@dataclass(frozen=True)
class TurnSettled(AgentEvent):
    """Subscriber publication of a settled ACP turn, distinct from the stream."""

    turn_id: TurnId


class AgentEventConsumer[Client: RuntimeInfoSource[RuntimeInfoObservation]](MroDispatch):
    """Shared activity and metadata algorithms; owners supply their context."""

    def __init__(self, *, comms: Client, thread_name: str):
        self._comms = comms
        self._thread_name = thread_name

    @property
    def comms(self) -> Client:
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

