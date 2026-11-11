from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.provider_records import ProviderDiagnosticError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import ToolCallId
from agent_comms.registry.activity import Activity, ActivityState, ObservedActivity

if TYPE_CHECKING:
    from agent_comms.execution.turn_lease import TurnLeaseFence
    from agent_comms.registry.agent_activity import AgentActivity


@dataclass(frozen=True)
class NativePhaseChanged(AgentEvent):
    """Actual native observer phase; never reconstructed from visible chunks."""
    phase: TurnPhase
    native_process: ProcessIdentity


class StallExempt:
    """Progress preserves this phase; an explicit transition replaces it."""

    def model_progress(self) -> TurnPhase:
        return self


class NoModelWait:
    """Native-owned excursions have no Core model-progress deadline."""

    def model_wait[Wait](self, backstop: Wait, unbounded: Wait) -> Wait:
        return unbounded


class AcceptsPrompt:
    """A native observation proves that Pi accepted the current prompt."""


class ClosingTurn:
    """Native tail observations cannot reopen a closing turn."""

    def accepts_followup(self) -> bool:
        return False

    def observed(self, phase: TurnPhase) -> Self:
        return self


class ActivityWord:
    """The activity alone names this phase: Ready while idle, Thinking on the model."""

    @property
    def activity_detail(self) -> str:
        return self.detail


@dataclass(frozen=True)
class TurnPhase(DeclaredFamily, affix="Phase"):
    """One phase owns watchdog behavior and its public control projection.

    A phase describes progress; it cannot permit native input or replay.
    The existing registry turn lease supplies identity independently of phase.
    """
    detail: str = ""

    stall_reason: ClassVar[str] = "model_no_progress"
    # An excursion (tool, retry, compaction) runs without RPC events; an input
    # queued behind it cannot start yet, so its start budget waits and the
    # excursion's own backstop bounds the stall instead.
    busy: ClassVar[bool] = True

    def pauses_input_clock(self) -> bool:
        return False

    def accepts_followup(self) -> bool:
        return True

    def keeps_project_resume(self) -> bool:
        return True

    def holds_refusal_reports(self) -> bool:
        return False
    activity_state: ClassVar[ActivityState] = ActivityState.THINKING
    label: ClassVar[str] = "Waiting for model"

    @property
    def summary(self) -> str:
        return self.detail or self.label

    @property
    def activity_detail(self) -> str:
        return self.summary

    def activity_event(self, thread: str) -> Activity:
        """Record this phase's declared activity, without observation-only readiness."""
        return Activity.create(thread, self.activity_state, self.activity_detail)

    def project_activity(self, activity: ObservedActivity) -> ObservedActivity:
        """Display this phase while preserving the acquired drain readiness."""
        return activity.with_status(self.activity_state, self.activity_detail)

    def model_wait[Wait](self, backstop: Wait, unbounded: Wait) -> Wait:
        """Select the actual wait capability rather than flatten it to a nullable timeout."""
        return backstop

    def input_started(self) -> TurnPhase:
        return ModelWaitPhase()

    def tool_started(self, tool_id: ToolCallId, title: str) -> TurnPhase:
        return ToolRunningPhase(tools=((tool_id, title),))

    def tool_ended(self, tool_id: ToolCallId) -> TurnPhase:
        return ModelWaitPhase()

    def compacting(self) -> TurnPhase:
        return CompactionPhase(resume=self)

    def compaction_ended(self) -> TurnPhase:
        return self

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return phase

    def model_progress(self) -> TurnPhase:
        return ModelWaitPhase()

    def stalled(self) -> TurnPhase:
        return self

    def stall_message(self, budget: float) -> str:
        return f"Model produced no RPC progress for {budget:g} seconds."

    def phase_effects(self, agents: AgentActivity, lease: TurnLeaseFence) -> None:
        agents.enter_phase(lease.identity.incarnation.name, self)


class Excursion(TurnPhase, declared_name=None):
    def pauses_input_clock(self) -> bool:
        return True


class PromptAcceptancePhase(TurnPhase):
    activity_state = ActivityState.STARTING
    stall_reason = "prompt_acceptance_timeout"
    label = "Waiting for input start"

    def stall_message(self, budget: float) -> str:
        return f"Pi did not accept the prompt within {budget:g} seconds."


class PreparingPhase(TurnPhase):
    activity_state = ActivityState.STARTING
    label = "Preparing input"


class IdlePhase(ActivityWord, TurnPhase):
    busy = False

    def accepts_followup(self) -> bool:
        return False
    activity_state = ActivityState.IDLE
    label = "Ready"

    def phase_effects(self, agents: AgentActivity, lease: TurnLeaseFence) -> None:
        agents.finish_turn(lease)


@dataclass(frozen=True, kw_only=True)
class FailedPhase(ClosingTurn, TurnPhase):
    """The provider failed this turn; its owner reports the error and ends the turn."""

    error: ProviderDiagnosticError
    label = "Provider failed"


class CancellingPhase(ClosingTurn, TurnPhase):
    def keeps_project_resume(self) -> bool:
        return False
    label = "Cancelling"


class ShutdownPhase(ClosingTurn, TurnPhase):
    """Native shutdown cannot allow follow-ups or resume model activity."""

    activity_state = ActivityState.WORKING
    label = "Stopping"


class PublishingPhase(ClosingTurn, TurnPhase):
    label = "Finishing turn"


class ModelWaitPhase(StallExempt, ActivityWord, TurnPhase):
    pass


class SettlingStatsPhase(TurnPhase):
    def accepts_followup(self) -> bool:
        return False
    label = "Finishing native statistics"


@dataclass(frozen=True)
class CompactionPhase(StallExempt, NoModelWait, Excursion):
    """Pi streams the summary without RPC events; provider idle bounds its request."""

    resume: TurnPhase = field(default_factory=ModelWaitPhase)

    activity_state = ActivityState.WORKING
    label = "Compacting context"

    def compacting(self) -> TurnPhase:
        return self

    def input_started(self) -> TurnPhase:
        return replace(self, resume=self.resume.input_started())

    def tool_started(self, tool_id: ToolCallId, title: str) -> TurnPhase:
        return replace(self, resume=self.resume.tool_started(tool_id, title))

    def tool_ended(self, tool_id: ToolCallId) -> TurnPhase:
        return replace(self, resume=self.resume.tool_ended(tool_id))

    def compaction_ended(self) -> TurnPhase:
        return self.resume


class ProviderRetryPhase(StallExempt, Excursion):
    stall_reason = "retry_no_progress"
    label = "Provider retry"
    # The recovery re-requests each refused empty response; its end event
    # reports the outcome, so a retried refusal never reports interim.
    def holds_refusal_reports(self) -> bool:
        return True


class SummarizationRetryPhase(StallExempt, Excursion):
    stall_reason = "summarization_retry_no_progress"
    label = "Summary retry"


@dataclass(frozen=True)
class ToolRunningPhase(StallExempt, NoModelWait, Excursion):
    """A running tool has no model budget; its terminal event ends the excursion."""

    tools: tuple[tuple[ToolCallId, str], ...] = ()
    activity_state = ActivityState.WORKING
    label = "Running tool"

    @property
    def summary(self) -> str:
        return self.detail or (self.tools[-1][1] if self.tools else self.label)

    def tool_started(self, tool_id: ToolCallId, title: str) -> TurnPhase:
        retained = tuple(item for item in self.tools if item[0] != tool_id)
        return replace(self, detail="", tools=(*retained, (tool_id, title)))

    def tool_ended(self, tool_id: ToolCallId) -> TurnPhase:
        retained = tuple(item for item in self.tools if item[0] != tool_id)
        return replace(self, tools=retained) if retained else ModelWaitPhase()
    def stalled(self) -> TurnPhase:
        return ModelWaitPhase()
