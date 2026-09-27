"""Goal lifecycle and pause provenance: the determining domain authorities."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState


@dataclass(frozen=True)
class PauseSource(DeclaredFamily, affix="Pause"):
    def instruction(self) -> str | None:
        return None

    @property
    def protects_pause(self) -> bool:
        return self.instruction() is not None

    def failure_projection(self) -> tuple[str, str]:
        return ("owner_paused", "owner_pause") if self.protects_pause else (
            "paused_uncertain", "pause_attribution_uncertain"
        )


class OwnerPause(PauseSource):
    def instruction(self) -> str:
        return (
            "This goal was paused by the owner. Do not resume or continue it; "
            "wait for the owner to explicitly resume it using the goal controls."
        )


class ModelPause(PauseSource):
    pass


class RuntimePause(PauseSource):
    pass


@dataclass(frozen=True)
class GoalState(DeclaredFamily, LifecycleState, affix="Goal"):
    active: ClassVar[bool] = False
    terminal: ClassVar[bool] = False
    toggle: ClassVar[str]
    toggle_label: ClassVar[str]
    acp_plan_status: ClassVar[str] = "in_progress"

    @classmethod
    def from_legacy(cls, status: str, reason: str | None, source: str | None) -> GoalState:
        return cls.decode(status).load(reason, source)

    @classmethod
    def load(cls, reason: str | None, source: str | None) -> GoalState:
        if reason is not None:
            raise ValueError("A blocked goal requires a bounded explicit reason.")
        return cls()

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[GoalState], ...]: ...

    def check_transition(self, nxt: GoalState, *, owner: bool) -> None:
        if not self.may_become(nxt):
            raise ValueError(self.transition_refusal())

    def transition_refusal(self) -> str:
        return "This goal cannot make that transition."

    @property
    def reason(self) -> str | None:
        return None

    @property
    def pause_source(self) -> PauseSource | None:
        return None

    @property
    def protected(self) -> bool:
        return False

    @property
    def execution_name(self) -> str:
        return self.declared_name

    def presentation(self) -> tuple[str, str]:
        return "✓", self.execution_name.title()


@dataclass(frozen=True)
class ActiveGoal(GoalState):
    active = True
    toggle = "paused"
    toggle_label = "Pause"

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return ActiveGoal, PausedGoal, BlockedGoal, CompletedGoal

    @property
    def execution_name(self) -> str:
        return "runnable"


@dataclass(frozen=True)
class PausedGoal(GoalState):
    source: PauseSource = field(default_factory=OwnerPause)
    toggle = "active"
    toggle_label = "Resume"

    @classmethod
    def load(cls, reason: str | None, source: str | None) -> PausedGoal:
        if reason is not None:
            raise ValueError("A blocked goal requires a bounded explicit reason.")
        return cls(PauseSource.decode(source)() if source is not None else OwnerPause())

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return ActiveGoal, PausedGoal, BlockedGoal, CompletedGoal

    @property
    def pause_source(self) -> PauseSource:
        return self.source

    @property
    def protected(self) -> bool:
        return self.source.protects_pause

    def check_transition(self, nxt: GoalState, *, owner: bool) -> None:
        super().check_transition(nxt, owner=owner)
        if self.protected and not owner:
            raise ValueError(self.source.instruction())


@dataclass(frozen=True)
class BlockedGoal(GoalState):
    block_reason: str | None = None  # legacy rows can lack a reason
    toggle = "retry"
    toggle_label = "Retry"

    def __post_init__(self) -> None:
        reason = self.block_reason
        if reason is not None and (
            type(reason) is not str or not reason.strip() or reason != reason.strip()
            or len(reason) > 1024
        ):
            raise ValueError("A blocked goal requires a bounded explicit reason.")

    @classmethod
    def load(cls, reason: str | None, source: str | None) -> BlockedGoal:
        return cls(reason)

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return (BlockedGoal,)

    def transition_refusal(self) -> str:
        return "Blocked goal requires an explicit retry through its owner."

    @property
    def reason(self) -> str | None:
        return self.block_reason

    def presentation(self) -> tuple[str, str]:
        reason = " ".join(self.block_reason.split()) if self.block_reason else "reason unavailable"
        summary = reason[:157] + "…" if len(reason) > 160 else reason
        return "!", f"Blocked · {summary}"


@dataclass(frozen=True)
class CompletedGoal(GoalState):
    terminal = True
    toggle = ""
    toggle_label = "Completed"

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return (CompletedGoal,)

    def transition_refusal(self) -> str:
        return "A completed goal cannot be resumed; set a new goal."
