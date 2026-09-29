"""Goal lifecycle and pause provenance: the determining domain authorities."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal

from .declared_family import DeclaredFamily
from .goal_attempt_identity import FailureNotObserved
from .lifecycle import LifecycleState

if TYPE_CHECKING:
    from .goal_actions import GoalAction


@dataclass(frozen=True)
class PauseSource(DeclaredFamily, affix="Pause"):
    def instruction(self) -> str | None:
        return None

    @property
    def protects_pause(self) -> bool:
        return self.instruction() is not None

    def failure_projection(self) -> tuple[Literal["owner_paused", "paused_uncertain"], str]:
        return (
            ("owner_paused", "owner_pause")
            if self.protects_pause
            else ("paused_uncertain", "pause_attribution_uncertain")
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

    def require_active(self) -> None:
        from .errors import RelationViolationError

        raise RelationViolationError("The executing goal is not active")

    def failure_projection(self, reason: str) -> tuple[str, str]:
        raise FailureNotObserved("owner_or_goal_changed")

    @property
    @abstractmethod
    def toggle(self) -> type[GoalAction] | None: ...

    toggle_label: ClassVar[str]
    acp_plan_status: ClassVar[str] = "in_progress"

    @classmethod
    def wire_payload(cls, reason: str | None, source: str | None) -> dict[str, object]:
        if reason is not None:
            raise ValueError("A blocked goal requires a bounded explicit reason.")
        return {"kind": cls.declared_name}

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


class FromOpenGoal:
    """A state explicitly eligible as the target of an active/paused transition."""


class OpenGoal(GoalState):
    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return GoalState.members_with(FromOpenGoal)


@dataclass(frozen=True)
class ActiveGoal(OpenGoal, FromOpenGoal):
    active = True

    def require_active(self) -> None:
        pass

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import PausedGoalAction

        return PausedGoalAction

    toggle_label = "Pause"

    @property
    def execution_name(self) -> str:
        return "runnable"


@dataclass(frozen=True)
class PausedGoal(OpenGoal, FromOpenGoal):
    source: PauseSource = field(default_factory=OwnerPause)

    def failure_projection(self, reason: str) -> tuple[str, str]:
        return self.source.failure_projection()

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import ActiveGoalAction

        return ActiveGoalAction

    toggle_label = "Resume"

    @classmethod
    def wire_payload(cls, reason: str | None, source: str | None) -> dict[str, object]:
        if reason is not None:
            raise ValueError("A blocked goal requires a bounded explicit reason.")
        return {
            "kind": cls.declared_name,
            "source": {"kind": source if source is not None else OwnerPause.declared_name},
        }

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


class BlockedState(GoalState, FromOpenGoal):
    def failure_projection(self, reason: str) -> tuple[str, str]:
        return "backend_suspended", reason

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import RetryGoalAction

        return RetryGoalAction

    toggle_label = "Retry"

    @property
    @abstractmethod
    def reason(self) -> str | None: ...

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return (BlockedGoal,)

    def transition_refusal(self) -> str:
        return "Blocked goal requires an explicit retry through its owner."


@dataclass(frozen=True)
class BlockedGoal(BlockedState):
    block_reason: str

    def __post_init__(self) -> None:
        reason = self.block_reason
        if (
            type(reason) is not str
            or not reason.strip()
            or reason != reason.strip()
            or len(reason) > 1024
        ):
            raise ValueError("A blocked goal requires a bounded explicit reason.")

    @classmethod
    def wire_payload(cls, reason: str | None, source: str | None) -> dict[str, object]:
        return {"kind": cls.declared_name, "block_reason": reason}

    @property
    def reason(self) -> str:
        return self.block_reason

    def presentation(self) -> tuple[str, str]:
        reason = " ".join(self.block_reason.split())
        summary = reason[:157] + "…" if len(reason) > 160 else reason
        return "!", f"Blocked · {summary}"


@dataclass(frozen=True)
class UnrecordedBlockGoal(BlockedState):
    """A recorded blocked status whose reason was not recorded."""

    @property
    def reason(self) -> None:
        return None

    def presentation(self) -> tuple[str, str]:
        return "!", "Blocked · reason unavailable"


@dataclass(frozen=True)
class CompletedGoal(GoalState, FromOpenGoal):
    terminal = True

    @property
    def toggle(self) -> None:
        return None

    toggle_label = "Completed"

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return (CompletedGoal,)

    def transition_refusal(self) -> str:
        return "A completed goal cannot be resumed; set a new goal."
