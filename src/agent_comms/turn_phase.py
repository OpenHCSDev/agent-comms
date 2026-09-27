"""Declaration-owned Pi excursions and phase-specific watchdog diagnostics."""

from __future__ import annotations

from typing import ClassVar

from . import pi_events as pi
from .declared_family import DeclaredFamily


class StallExempt:
    """Model progress does not end this excursion; its own events do."""


class TurnPhase(DeclaredFamily, affix="Phase"):
    start: ClassVar[tuple[type[pi.PiEvent], ...]] = ()
    end: ClassVar[tuple[type[pi.PiEvent], ...]] = ()
    stall_reason: ClassVar[str] = "model_no_progress"
    pauses_input_clock: ClassVar[bool] = False

    def on(self, event: pi.PiEvent, active_tools: set[str]) -> TurnPhase:
        for member in TurnPhase.members_with(TurnPhase):
            if isinstance(event, member.start):
                return member()
        if isinstance(event, self.end) and not active_tools:
            return ModelWaitPhase()
        return self

    def model_progress(self) -> TurnPhase:
        return ModelWaitPhase()

    def stalled(self, accepted: bool) -> tuple[str, str]:
        phase = self if accepted else PromptAcceptancePhase()
        return phase.stall_reason, phase.declared_name


class Excursion(TurnPhase):
    def model_progress(self) -> TurnPhase:
        return self


class PromptAcceptancePhase(TurnPhase):
    stall_reason = "prompt_acceptance_timeout"


class ModelWaitPhase(TurnPhase):
    pass


class SettlingStatsPhase(TurnPhase):
    pass


class CompactionPhase(Excursion, StallExempt):
    start = (pi.CompactionStart, pi.CompactionProgress)
    end = (pi.CompactionEnd,)
    stall_reason = "compaction_no_progress"
    pauses_input_clock = True


class ProviderRetryPhase(Excursion, StallExempt):
    start = (pi.AutoRetryStart,)
    end = (pi.AutoRetryEnd,)
    stall_reason = "retry_no_progress"


class SummarizationRetryPhase(Excursion, StallExempt):
    start = (pi.SummarizationRetryScheduled, pi.SummarizationRetryAttemptStart)
    end = (pi.SummarizationRetryFinished,)
    stall_reason = "summarization_retry_no_progress"


class ToolRunningPhase(Excursion):
    start = (pi.ToolExecutionStart,)
    end = (pi.ToolExecutionEnd,)

    def stalled(self, accepted: bool) -> tuple[str, str]:
        return ModelWaitPhase().stalled(accepted)
