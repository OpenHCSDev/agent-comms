"""Stored goal decisions; backend execution is read from the attempt ledger."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState

if TYPE_CHECKING:
    from .goal_actions import GoalActionContext, ProblemOrderGoalAction
    from .goal_presentation import GoalExecution, GoalExecutionSource
    from .goals import Goal


@dataclass(frozen=True)
class PauseSource(DeclaredFamily, affix="Pause"):
    def instruction(self) -> str | None:
        return None

    @property
    def protects_pause(self) -> bool:
        return self.instruction() is not None


class OwnerPause(PauseSource):
    def instruction(self) -> str:
        from .turn_context import InstructionFile
        return InstructionFile.read("owner-pause.md").content


class ModelPause(PauseSource):
    pass


class RuntimePause(PauseSource):
    pass


@dataclass(frozen=True)
class GoalState(DeclaredFamily, LifecycleState, affix="Goal"):
    active: ClassVar[bool] = False
    terminal: ClassVar[bool] = False
    acp_plan_status: ClassVar[str] = "in_progress"

    @abstractmethod
    def execution(self, source: GoalExecutionSource) -> GoalExecution: ...

    def accept_order(self, action: ProblemOrderGoalAction, goal: Goal, ctx: GoalActionContext) -> Goal:
        return action.continue_goal(goal, ctx)

    def require_model_resume(self) -> None:
        raise ValueError("This goal cannot be resumed; refresh its state.")

    def require_active(self) -> None:
        from .errors import RelationViolationError
        raise RelationViolationError(self.activity_refusal())

    def activity_refusal(self) -> str:
        pause = self.pause_source
        return ((pause.instruction() if pause else None)
                or "This goal is no longer active; refresh its state.")

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


class FromOpenGoal:
    """A state eligible as the target of an active/paused decision."""


class OpenGoal(GoalState):
    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return GoalState.members_with(FromOpenGoal)


@dataclass(frozen=True)
class ActiveGoal(OpenGoal, FromOpenGoal):
    active = True

    def require_active(self) -> None:
        pass

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        return source.wait.execution(source)


@dataclass(frozen=True)
class PausedGoal(OpenGoal, FromOpenGoal):
    source: PauseSource = field(default_factory=OwnerPause)

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        from .goal_presentation import PausedExecution
        return PausedExecution(source.goal.id, self.source)

    @classmethod
    def wire_payload(cls, reason: str | None, source: str | None) -> dict[str, object]:
        if reason is not None:
            raise ValueError("A blocked goal requires a bounded explicit reason.")
        return {"kind": cls.declared_name,
                "source": {"kind": source if source is not None else OwnerPause.declared_name}}

    @property
    def pause_source(self) -> PauseSource:
        return self.source

    @property
    def protected(self) -> bool:
        return self.source.protects_pause

    def require_model_resume(self) -> None:
        if instruction := self.source.instruction():
            raise ValueError(instruction)

    def check_transition(self, nxt: GoalState, *, owner: bool) -> None:
        super().check_transition(nxt, owner=owner)
        if self.protected and not owner:
            raise ValueError(self.source.instruction())


class BlockedState(GoalState, FromOpenGoal):
    def accept_order(self, action: ProblemOrderGoalAction, goal: Goal, ctx: GoalActionContext) -> Goal:
        ordered = action.continue_goal(goal, ctx)
        ctx.goals.open_goal_store().authorize_current_retry(goal.id, action.order.id)
        return replace(ordered, state=ActiveGoal())

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return (BlockedGoal,)

    def activity_refusal(self) -> str:
        return self.transition_refusal()

    def transition_refusal(self) -> str:
        return "Blocked goal requires an explicit retry."


@dataclass(frozen=True)
class BlockedGoal(BlockedState):
    block_reason: str

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        from .goal_presentation import DecidedBlockExecution
        return DecidedBlockExecution(source.goal.id, self.block_reason)

    def __post_init__(self) -> None:
        reason = self.block_reason
        if (type(reason) is not str or not reason.strip() or reason != reason.strip()
                or len(reason) > 1024):
            raise ValueError("A blocked goal requires a bounded explicit reason.")

    @classmethod
    def wire_payload(cls, reason: str | None, source: str | None) -> dict[str, object]:
        return {"kind": cls.declared_name, "block_reason": reason}

    @property
    def reason(self) -> str:
        return self.block_reason


@dataclass(frozen=True)
class UnrecordedBlockGoal(BlockedState):
    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        from .goal_presentation import UnrecordedBlockExecution
        return UnrecordedBlockExecution(source.goal.id)


@dataclass(frozen=True)
class CompletedGoal(GoalState, FromOpenGoal):
    terminal = True

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        from .goal_presentation import CompletedExecution
        return CompletedExecution(source.goal.id)

    def accept_order(self, action: ProblemOrderGoalAction, goal: Goal, ctx: GoalActionContext) -> Goal:
        goal.order.require_replacement(goal, ctx.goals, ctx.thread.incarnation)
        return action.create(ctx)

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return (CompletedGoal,)

    def transition_refusal(self) -> str:
        return "A completed goal cannot be resumed; set a new goal."
