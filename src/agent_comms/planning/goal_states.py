from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from agent_comms.instruction_bytes import InstructionBytes
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Active, Complete, Terminal
from agent_comms.planning.goal_mutation import GoalStateOrder, GoalStateProjection
from agent_comms.planning.goal_work import GoalOwner
from agent_comms.registry.thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class PauseSource(DeclaredFamily, affix="Pause"):
    lifts_on_compliance: ClassVar[bool] = False

    def instruction(self) -> str | None:
        return None

    @property
    def protects_pause(self) -> bool:
        return self.instruction() is not None

    def require_peer_change(self) -> None:
        """A teammate may change a goal under an unprotected pause; a protected pause names
        who lifts it, and a teammate is not that."""
        if instruction := self.instruction():
            raise ValueError(f"A teammate cannot change this goal while it is paused: {instruction}")


class OwnerPause(PauseSource):
    def instruction(self) -> str:
        return InstructionBytes.read("owner-pause.md").text


class ModelPause(PauseSource):
    pass


class RuntimePause(PauseSource):
    pass


class ChokeholdPause(PauseSource):
    """The heartbeat held this goal because its owner ignored a vibe check; Core resumes it
    when that owner complies, and nothing else may."""

    lifts_on_compliance = True

    def instruction(self) -> str:
        return (
            "You ignored Tristan's vibe check, so Tristan paused this goal until you comply."
            " Do not resume or continue it; Core resumes it when your answer quotes the"
            " pattern catalog."
        )


@dataclass(frozen=True)
class GoalState(DeclaredFamily, LifecycleState, affix="Goal"):
    acp_plan_status: ClassVar[str] = "in_progress"

    @abstractmethod
    def execution[Execution](
        self, source: GoalStateProjection[Execution, PauseSource],
    ) -> Execution: ...

    def accept_order[Subject, Context](
        self, action: GoalStateOrder[Subject, Context], goal: Subject, ctx: Context,
    ) -> Subject:
        return action.continue_goal(goal, ctx)

    def require_work(self, actor: ThreadIncarnation, owner: GoalOwner) -> None:
        owner.require_goal_assignment(actor)

    def work_candidates[Owner](self, thread: Owner) -> tuple[Owner, ...]:
        return ()

    def require_model_resume(self) -> None:
        raise ValueError("This goal cannot be resumed; refresh its state.")

    def activity_refusal(self) -> str:
        pause = self.pause_source
        return ((pause.instruction() if pause else None)
                or "This goal is no longer active; refresh its state.")

    def check_transition(self, nxt: GoalState, *, owner: bool) -> None:
        if not self.may_become(nxt):
            raise ValueError(self.transition_refusal())

    def continues_turn_from(self, captured: GoalState) -> bool:
        """A running turn keeps its authority only while the state it was granted stands."""
        return self == captured

    def held_stop(self, text: str) -> tuple[str, ...]:
        """Why the owner's session may not end its turn yet: only an active goal holds it."""
        return ()

    def transition_refusal(self) -> str:
        return "This goal cannot make that transition."

    @property
    def reason(self) -> str | None:
        return None

    def blocked_reasons(self) -> tuple[str, ...]:
        """Only a blocked state contributes a teammate warning."""
        return ()

    @property
    def pause_source(self) -> PauseSource | None:
        return None

    def require_peer_change(self) -> None:
        """Only a protected pause withholds a goal from its teammates."""

    @property
    def lifts_on_compliance(self) -> bool:
        """Held by the heartbeat until the teammate complies."""
        return False

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
class ActiveGoal(Active, OpenGoal, FromOpenGoal):

    def execution[Execution](
        self, source: GoalStateProjection[Execution, PauseSource],
    ) -> Execution:
        return source.active()

    def continues_turn_from(self, captured: GoalState) -> bool:
        """An order or retry that makes the goal active never ends a running turn."""
        return True

    def held_stop(self, text: str) -> tuple[str, ...]:
        return (f"Your goal is still active: {text}\n"
                "Keep working on it, and report progress or completion with comms_goal. "
                "A background job still running is no reason to end the turn: do the next step.",)


@dataclass(frozen=True)
class PausedGoal(OpenGoal, FromOpenGoal):
    source: PauseSource = field(default_factory=OwnerPause)

    def execution[Execution](
        self, source: GoalStateProjection[Execution, PauseSource],
    ) -> Execution:
        return source.paused(self.source)

    @property
    def pause_source(self) -> PauseSource:
        return self.source

    def require_peer_change(self) -> None:
        self.source.require_peer_change()

    @property
    def lifts_on_compliance(self) -> bool:
        return self.source.lifts_on_compliance

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

    def require_work(self, actor: ThreadIncarnation, owner: GoalOwner) -> None:
        self.require_model_resume()
        super().require_work(actor, owner)

    def accept_order[Subject, Context](
        self, action: GoalStateOrder[Subject, Context], goal: Subject, ctx: Context,
    ) -> Subject:
        """A pause holds off every order, a correction of one the goal already serves included."""
        self.require_model_resume()
        return super().accept_order(action, goal, ctx)


class BlockedState(GoalState, FromOpenGoal, declared_name=None):
    @property
    def reason(self) -> str:
        return "reason unavailable"

    def execution[Execution](
        self, source: GoalStateProjection[Execution, PauseSource],
    ) -> Execution:
        return source.blocked(self.reason)

    def blocked_reasons(self) -> tuple[str, ...]:
        return (self.reason,)

    def accept_order[Subject, Context](
        self, action: GoalStateOrder[Subject, Context], goal: Subject, ctx: Context,
    ) -> Subject:
        return action.reactivate_goal(goal, ctx)

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

    def __post_init__(self) -> None:
        reason = self.block_reason
        if (type(reason) is not str or not reason.strip() or reason != reason.strip()
                or len(reason) > 1024):
            raise ValueError("A blocked goal requires a bounded explicit reason.")

    @property
    def reason(self) -> str:
        return self.block_reason


@dataclass(frozen=True)
class UnrecordedBlockGoal(BlockedState):
    """A carried block whose reason was never recorded; its state says so, nothing invents one."""


@dataclass(frozen=True)
class CompletedGoal(Complete, Terminal, GoalState, FromOpenGoal):

    def require_work(self, actor: ThreadIncarnation, owner: GoalOwner) -> None:
        """Completion frees the goal slot; held Todo and parked-thread fences still apply."""

    def work_candidates[Owner](self, thread: Owner) -> tuple[Owner, ...]:
        return (thread,)

    def execution[Execution](
        self, source: GoalStateProjection[Execution, PauseSource],
    ) -> Execution:
        return source.completed()

    def accept_order[Subject, Context](
        self, action: GoalStateOrder[Subject, Context], goal: Subject, ctx: Context,
    ) -> Subject:
        return action.replace_goal(goal, ctx)

    @classmethod
    def successors(cls) -> tuple[type[GoalState], ...]:
        return (CompletedGoal,)

    def transition_refusal(self) -> str:
        return "A completed goal cannot be resumed; set a new goal."
