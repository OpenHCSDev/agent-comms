"""Shared turn stages own activity and phase-specific watchdog behavior."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import ClassVar, TYPE_CHECKING

from .declared_family import DeclaredFamily
from .activity import Activity, ActivityState, ObservedActivity
from .pi_payloads import ProviderDiagnosticError
if TYPE_CHECKING:
    from .agent_activity import AgentActivity
    from .turn_lease import TurnLeaseFence


class StallExempt:
    """Model progress does not end this excursion; its own events do."""

    def model_progress(self) -> TurnPhase:
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
    pauses_input_clock: ClassVar[bool] = False
    busy: ClassVar[bool] = True
    accepts_prompt: ClassVar[bool] = False
    accepts_followup: ClassVar[bool] = True
    can_compact: ClassVar[bool] = False
    can_cancel: ClassVar[bool] = True
    # A turn released in this phase still owes the turn after its project change.
    keeps_project_resume: ClassVar[bool] = True
    # Refusal reports wait while a retry excursion owns the turn's outcome.
    holds_refusal_reports: ClassVar[bool] = False
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

    def model_wait(self, backstop: float | None) -> float | None:
        """Core's no-event backstop for this phase; None where nothing bounds it here."""
        return backstop

    def input_started(self) -> TurnPhase:
        return ModelWaitPhase()

    def tool_started(self, tool_id: str, title: str) -> TurnPhase:
        return ToolRunningPhase(tools=((tool_id, title),))

    def tool_ended(self, tool_id: str) -> TurnPhase:
        return ModelWaitPhase()

    def compacting(self) -> TurnPhase:
        return CompactionPhase(resume=self)

    def compaction_ended(self) -> TurnPhase:
        return self

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return phase

    def may_finish(self, active_tools: set[str]) -> bool:
        return True

    def model_progress(self) -> TurnPhase:
        return ModelWaitPhase()

    def response_output(self) -> TurnPhase:
        """Response deltas are progress evidence, not a persisted control phase."""
        return self

    def stalled(self, accepted: bool) -> TurnPhase:
        return self if accepted else PromptAcceptancePhase()

    def phase_effects(self, agents: AgentActivity, lease: TurnLeaseFence) -> None:
        agents.set_activity(
            lease.identity.incarnation.name, self.activity_state, self.activity_detail,
        )


class Excursion(TurnPhase, declared_name=None):
    pauses_input_clock = True


class PromptAcceptancePhase(TurnPhase):
    activity_state = ActivityState.STARTING
    stall_reason = "prompt_acceptance_timeout"
    label = "Waiting for input start"


class PreparingPhase(TurnPhase):
    activity_state = ActivityState.STARTING
    label = "Preparing input"


class IdlePhase(ActivityWord, TurnPhase):
    busy = False
    accepts_prompt = True
    accepts_followup = False
    can_compact = True
    can_cancel = False
    activity_state = ActivityState.IDLE
    label = "Ready"

    def phase_effects(self, agents: AgentActivity, lease: TurnLeaseFence) -> None:
        agents.finish_turn(lease)


@dataclass(frozen=True, kw_only=True)
class FailedPhase(IdlePhase):
    error: ProviderDiagnosticError


class CancellingPhase(TurnPhase):
    accepts_followup = False
    keeps_project_resume = False
    label = "Cancelling"
    can_cancel = False

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return self

class ShutdownPhase(TurnPhase):
    """Native shutdown cannot allow follow-ups or resume model activity."""

    accepts_followup = False
    can_cancel = False
    activity_state = ActivityState.WORKING
    label = "Stopping"

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return self


class PublishingPhase(TurnPhase):
    accepts_followup = False
    label = "Finishing turn"
    can_cancel = False

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return self


class ModelWaitPhase(ActivityWord, TurnPhase):
    def model_progress(self) -> TurnPhase:
        return self


class SettlingStatsPhase(TurnPhase):
    accepts_followup = False
    label = "Finishing native statistics"


@dataclass(frozen=True)
class CompactionPhase(StallExempt, Excursion):
    resume: TurnPhase = field(default_factory=ModelWaitPhase)

    activity_state = ActivityState.WORKING
    label = "Compacting context"

    def compacting(self) -> TurnPhase:
        return self

    def model_wait(self, backstop: float | None) -> float | None:
        # Pi streams the summary without RPC events; its provider idle timeout
        # bounds the request and compaction_end reports the outcome.
        return None

    def input_started(self) -> TurnPhase:
        return replace(self, resume=self.resume.input_started())

    def tool_started(self, tool_id: str, title: str) -> TurnPhase:
        return replace(self, resume=self.resume.tool_started(tool_id, title))

    def tool_ended(self, tool_id: str) -> TurnPhase:
        return replace(self, resume=self.resume.tool_ended(tool_id))

    def compaction_ended(self) -> TurnPhase:
        return self.resume


class ProviderRetryPhase(StallExempt, Excursion):
    stall_reason = "retry_no_progress"
    label = "Provider retry"
    # The recovery re-requests each refused empty response; its end event
    # reports the outcome, so a retried refusal never reports interim.
    holds_refusal_reports = True


class SummarizationRetryPhase(StallExempt, Excursion):
    stall_reason = "summarization_retry_no_progress"
    label = "Summary retry"


@dataclass(frozen=True)
class ToolRunningPhase(Excursion):
    tools: tuple[tuple[str, str], ...] = ()
    activity_state = ActivityState.WORKING
    label = "Running tool"

    @property
    def summary(self) -> str:
        return self.detail or (self.tools[-1][1] if self.tools else self.label)

    def tool_started(self, tool_id: str, title: str) -> TurnPhase:
        retained = tuple(item for item in self.tools if item[0] != tool_id)
        return replace(self, detail="", tools=(*retained, (tool_id, title)))

    def tool_ended(self, tool_id: str) -> TurnPhase:
        retained = tuple(item for item in self.tools if item[0] != tool_id)
        return replace(self, tools=retained) if retained else ModelWaitPhase()
    def may_finish(self, active_tools: set[str]) -> bool:
        return not active_tools

    def stalled(self, accepted: bool) -> TurnPhase:
        return ModelWaitPhase().stalled(accepted)
