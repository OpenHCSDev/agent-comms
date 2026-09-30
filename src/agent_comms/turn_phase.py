"""Declaration-owned Pi excursions and phase-specific watchdog diagnostics."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from time import time
from typing import ClassVar, TYPE_CHECKING

from .declared_family import DeclaredFamily
from .activity import ActivityState
from .compaction_progress import CompactionSourceProgress

if TYPE_CHECKING:
    from . import pi_events as pi


class StallExempt:
    """Model progress does not end this excursion; its own events do."""

    def model_progress(self) -> TurnPhase:
        return self


@dataclass(frozen=True)
class TurnPhase(DeclaredFamily, affix="Phase"):
    """One phase owns watchdog behavior and its public control projection.

    A phase is an observation, never native input admission or replay authority.
    The existing registry turn lease supplies identity independently of phase.
    """
    detail: str = ""
    stall_reason: ClassVar[str] = "model_no_progress"
    pauses_input_clock: ClassVar[bool] = False
    busy: ClassVar[bool] = True
    accepts_prompt: ClassVar[bool] = False
    accepts_followup: ClassVar[bool] = True
    can_compact: ClassVar[bool] = False
    can_cancel: ClassVar[bool] = True
    activity_state: ClassVar[ActivityState] = ActivityState.THINKING
    label: ClassVar[str] = "Waiting for model"

    @property
    def summary(self) -> str:
        return self.detail or self.label

    def described(self, detail: str) -> TurnPhase:
        return replace(self, detail=detail[:200])

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

    def following_compaction(self, previous: CompactionPhase) -> TurnPhase:
        return self

    def on(self, event: pi.PiEvent, active_tools: set[str]) -> TurnPhase:
        for member in TurnPhase.members_with(TurnPhase):
            if member.starts(event):
                return member.enter(self, event, active_tools)
        if self.ends(event) and self.may_finish(active_tools):
            return ModelWaitPhase()
        return self

    @classmethod
    def starts(cls, event: pi.PiEvent) -> bool:
        return False

    @classmethod
    def enter(cls, current: TurnPhase, event: pi.PiEvent, active_tools: set[str]) -> TurnPhase:
        return cls()

    def ends(self, event: pi.PiEvent) -> bool:
        return False

    def may_finish(self, active_tools: set[str]) -> bool:
        return True

    def model_progress(self) -> TurnPhase:
        return ModelWaitPhase()

    def stalled(self, accepted: bool) -> tuple[str, str]:
        phase = self if accepted else PromptAcceptancePhase()
        return phase.stall_reason, phase.declared_name


class Excursion(TurnPhase):
    @classmethod
    @abstractmethod
    def starts(cls, event: pi.PiEvent) -> bool:
        """Concrete excursions declare their entry events."""


class PromptAcceptancePhase(TurnPhase):
    stall_reason = "prompt_acceptance_timeout"
    label = "Waiting for input start"


class PreparingPhase(TurnPhase):
    label = "Preparing input"


class IdlePhase(TurnPhase):
    busy = False
    accepts_prompt = True
    accepts_followup = False
    can_compact = True
    can_cancel = False
    activity_state = ActivityState.IDLE
    label = "Ready"


class CancellingPhase(TurnPhase):
    accepts_followup = False
    label = "Cancelling"
    can_cancel = False

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return self

class PublishingPhase(TurnPhase):
    accepts_followup = False
    label = "Finishing turn"
    can_cancel = False

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return self


class ModelWaitPhase(TurnPhase):
    pass


class SettlingStatsPhase(TurnPhase):
    accepts_followup = False
    label = "Finishing native statistics"


@dataclass(frozen=True)
class CompactionPhase(StallExempt, Excursion):
    resume: TurnPhase = field(default_factory=ModelWaitPhase)
    operation_id: str = ""
    started_at: float = field(default_factory=time)
    source: CompactionSourceProgress | None = None

    @property
    def summary(self) -> str:
        label = self.detail or self.label
        return f"{label} · {self.source.label}" if self.source is not None and self.source.label else label

    def measured(self, operation_id: str, source: CompactionSourceProgress | None) -> TurnPhase:
        if self.operation_id and operation_id and self.operation_id != operation_id:
            raise ValueError("Compaction progress belongs to another operation")
        return replace(self, operation_id=operation_id or self.operation_id,
                       source=source if source is not None else self.source)

    def observed(self, phase: TurnPhase) -> TurnPhase:
        return phase.following_compaction(self)

    def following_compaction(self, previous: CompactionPhase) -> TurnPhase:
        return replace(self, operation_id=previous.operation_id, started_at=previous.started_at,
                       source=self.source if self.source is not None else previous.source)
    @classmethod
    def starts(cls, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, (pi.CompactionStart, pi.CompactionProgress))

    @classmethod
    def enter(cls, current: TurnPhase, event: pi.PiEvent, active_tools: set[str]) -> TurnPhase:
        return current.compacting()

    def ends(self, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, pi.CompactionEnd)
    stall_reason = "compaction_no_progress"
    pauses_input_clock = True
    activity_state = ActivityState.WORKING
    label = "Compacting context"

    def compacting(self) -> TurnPhase:
        return self

    def input_started(self) -> TurnPhase:
        return replace(self, resume=self.resume.input_started())

    def tool_started(self, tool_id: str, title: str) -> TurnPhase:
        return replace(self, resume=self.resume.tool_started(tool_id, title))

    def tool_ended(self, tool_id: str) -> TurnPhase:
        return replace(self, resume=self.resume.tool_ended(tool_id))

    def compaction_ended(self) -> TurnPhase:
        return self.resume


class ProviderRetryPhase(StallExempt, Excursion):
    @classmethod
    def starts(cls, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, pi.AutoRetryStart)

    def ends(self, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, pi.AutoRetryEnd)
    stall_reason = "retry_no_progress"
    label = "Provider retry"


class SummarizationRetryPhase(StallExempt, Excursion):
    @classmethod
    def starts(cls, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, (pi.SummarizationRetryScheduled, pi.SummarizationRetryAttemptStart))

    def ends(self, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, pi.SummarizationRetryFinished)
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
        return replace(self, detail="", tools=(*retained, (tool_id, title[:200])))

    def tool_ended(self, tool_id: str) -> TurnPhase:
        retained = tuple(item for item in self.tools if item[0] != tool_id)
        return replace(self, tools=retained) if retained else ModelWaitPhase()
    def may_finish(self, active_tools: set[str]) -> bool:
        return not active_tools

    @classmethod
    def starts(cls, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, pi.ToolExecutionStart)

    def ends(self, event: pi.PiEvent) -> bool:
        from . import pi_events as pi
        return isinstance(event, pi.ToolExecutionEnd)

    @classmethod
    def enter(cls, current: TurnPhase, event: pi.PiEvent, active_tools: set[str]) -> TurnPhase:
        name = event.tool_name or "tool"
        return current.tool_started(event.tool_call_id or name, name)

    def on(self, event: pi.PiEvent, active_tools: set[str]) -> TurnPhase:
        if self.ends(event):
            return self.tool_ended(event.tool_call_id or event.tool_name or "tool")
        return super().on(event, active_tools)

    def stalled(self, accepted: bool) -> tuple[str, str]:
        return ModelWaitPhase().stalled(accepted)
