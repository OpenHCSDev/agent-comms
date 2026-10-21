"""Goal execution is a read of the decisions, wait, ledger and live owner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar, Self

from .field_codec import NullMember, RecordMember, SubclassUnion, WireValue
from .goal_failure_observation import FailedTurnEvidence
from .goal_states import PauseSource
from .goals import Goal, GoalCheckpoint, GoalDeclaration
from .thread_identity import RecordedThreadIncarnation
from .thread_presentation import ThreadPresentation

if TYPE_CHECKING:
    from .agent_activity import AgentActivity
    from .bus_publication import CommittedDelivery
    from .goal_actions import GoalAction
    from .goal_attempts import Generation
    from .goal_failure_observation import GoalFailureEvidence
    from .goal_waits import GoalWait
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .turn_lease import TurnDeclaration


@dataclass(frozen=True, slots=True)
class GoalWaitTarget(WireValue):
    incarnation: RecordedThreadIncarnation

    def sent(self, original: CommittedDelivery) -> bool:
        return original.audience.sender_lookup == self.incarnation.lookup

    def to_wire(self) -> dict[str, str | float]:
        return self.incarnation.to_wire()

    @classmethod
    def from_wire(cls, data: Mapping) -> GoalWaitTarget:
        return cls(RecordedThreadIncarnation.from_wire(data))

    @classmethod
    def schema(cls) -> dict:
        return RecordedThreadIncarnation.schema()


@dataclass(frozen=True)
class GoalExecutionSource:
    """One read's original owners; never persisted or used as launch authority."""

    goal: Goal
    wait: GoalWait
    generation: Generation
    evidence: GoalFailureEvidence
    owner: Thread
    snapshot: RegistrySnapshot
    agents: AgentActivity

    @cached_property
    def turn(self) -> TurnDeclaration:
        return self.owner.turn_record.read()

    @cached_property
    def owner_alive(self) -> bool:
        return self.owner.process_alive

    def execution(self) -> GoalExecution:
        return self.goal.state.execution(self)


class GoalExecution(SubclassUnion, affix="Execution"):
    """The complete execution family consumed by commands, views and the commander."""

    toggle_label: ClassVar[str] = "Pause"

    @property
    def toggle(self) -> type[GoalAction] | None:
        return None

    def details_heading(self, heading: str) -> str:
        return heading

    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return ordinary

    def standby(self) -> bool:
        return False

    def waiting_for(self, goal: Goal) -> bool:
        return False

    def unit_blocks(self) -> tuple[BlockedExecution, ...]:
        return ()

    @abstractmethod
    def validate_goal(self, goal: GoalCheckpoint) -> None: ...

    @abstractmethod
    def heading(self, revision: int) -> str: ...


@dataclass(frozen=True)
class IdleGoalExecution(NullMember, GoalExecution):
    def validate_goal(self, goal: GoalCheckpoint) -> None:
        pass

    def heading(self, revision: int) -> str:
        return "No current goal"


@dataclass(frozen=True)
class BoundGoalExecution(RecordMember, GoalExecution, declared_name=None):
    goal_id: str
    glyph: ClassVar[str] = "✓"
    label: ClassVar[str]

    @property
    def summary(self) -> str:
        return self.label

    def presentation(self, title: str) -> ThreadPresentation:
        return ThreadPresentation(title, self.glyph, self.summary)

    def heading(self, revision: int) -> str:
        return f"Goal · {self.label} · rev {revision}"

    def validate_goal(self, goal: GoalCheckpoint) -> None:
        goal.require_identity(self.goal_id)


class WaitingExecution(BoundGoalExecution, declared_name=None):
    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return self.presentation(ordinary.title)


@dataclass(frozen=True)
class RunnableExecution(BoundGoalExecution):
    label = "Active"

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import PausedGoalAction
        return PausedGoalAction


@dataclass(frozen=True)
class RunningExecution(RunnableExecution):
    label = "Running"


@dataclass(frozen=True)
class CompletedExecution(BoundGoalExecution):
    label = "Completed"


@dataclass(frozen=True)
class PausedExecution(WaitingExecution):
    source: PauseSource
    label = "Paused"
    toggle_label = "Resume"

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import ActiveGoalAction
        return ActiveGoalAction


@dataclass(frozen=True)
class StandbyExecution(WaitingExecution):
    wait_for: tuple[GoalWaitTarget, ...]
    inactive_wait_for: tuple[GoalWaitTarget, ...]
    label = "Standby"
    glyph = "◌"

    @property
    def summary(self) -> str:
        names = ", ".join(f"@{target.incarnation.name}" for target in self.wait_for)
        idle = ", ".join(f"@{target.incarnation.name}" for target in self.inactive_wait_for)
        suffix = f"; no active turn: {idle}" if idle else ""
        return f"Standby · waiting for {names}{suffix}"

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import PausedGoalAction
        return PausedGoalAction

    def details_heading(self, heading: str) -> str:
        return f"{heading} · {self.summary}"

    def standby(self) -> bool:
        return True

    def waiting_for(self, goal: Goal) -> bool:
        return goal.id == self.goal_id and goal.state.active


class BlockedExecution(WaitingExecution, declared_name=None):
    glyph = "!"
    label = "Blocked"
    toggle_label = "Retry"

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import RetryGoalAction
        return RetryGoalAction

    @property
    @abstractmethod
    def reason(self) -> str: ...

    @property
    def summary(self) -> str:
        reason = " ".join(self.reason.split())
        summary = reason[:157] + "…" if len(reason) > 160 else reason
        return f"Blocked · {summary}"

    def unit_blocks(self) -> tuple[BlockedExecution, ...]:
        return (self,)

    def unit_block_line(self, unit: str, orders: tuple[str, ...]) -> str:
        return (f"BLOCKED {unit}: orders {', '.join(orders)}; goal {self.goal_id}; "
                f"{self.reason} -> Core reissues the orders")

    def enforce_unit_block(self, comms, commander: str, unit: str, orders: tuple[str, ...]) -> None:
        from .commander.standing import UnownedWheelSpin
        from .goal_actions import GoalPrecondition
        from .thread_identity import CoreWatcherIncarnation

        if comms.goals.goal_execution(unit) != self:
            return
        observed = comms.registry.require(unit).goal.require_id(self.goal_id)
        for problem in orders:
            todo = comms.goals.todos.get(problem)
            todo.require_assignee(comms.registry.require(unit).incarnation, observed.id)
            action = UnownedWheelSpin.order_for(todo, commander, CoreWatcherIncarnation.watcher())
            observed = comms.goals.update_goal(unit, replace(
                action, expect=GoalPrecondition(
                    goal_id=observed.id, expected_state=observed.state,
                    expected_checkpoint=observed.captured,
                ),
            ))


@dataclass(frozen=True)
class DecidedBlockExecution(BlockedExecution):
    block_reason: str

    @property
    def reason(self) -> str:
        return self.block_reason


@dataclass(frozen=True)
class UnrecordedBlockExecution(BlockedExecution):
    @property
    def reason(self) -> str:
        return "reason unavailable"


@dataclass(frozen=True)
class AttemptUnresolvedExecution(BlockedExecution):
    @property
    def reason(self) -> str:
        return "inspect the unknown attempt; never replay"

    def unit_block_line(self, unit: str, orders: tuple[str, ...]) -> str:
        return (f"UNRESOLVED BLOCK {unit}: orders {', '.join(orders)}; "
                f"goal {self.goal_id} -> {self.reason}")

    def enforce_unit_block(self, comms, commander: str, unit: str, orders: tuple[str, ...]) -> None:
        pass


@dataclass(frozen=True)
class AttemptFailedExecution(BlockedExecution):
    evidence: FailedTurnEvidence

    @property
    def reason(self) -> str:
        return self.evidence.reason.value


class GoalPublicationSnapshot(ABC):
    @abstractmethod
    def changed_by(self, current: GoalSnapshot) -> bool: ...

    @abstractmethod
    def standby(self) -> bool: ...


class UnpublishedGoalSnapshot(NullMember, GoalPublicationSnapshot):
    def changed_by(self, current: GoalSnapshot) -> bool:
        return True

    def standby(self) -> bool:
        return False


@dataclass(frozen=True)
class GoalSnapshot(GoalPublicationSnapshot):
    goal: GoalDeclaration
    execution: GoalExecution

    def changed_by(self, current: GoalSnapshot) -> bool:
        return asdict(self) != asdict(current)

    def standby(self) -> bool:
        return self.execution.standby()

    @classmethod
    def from_snapshot(cls, snapshot: GoalSnapshot, *extra: object) -> Self:
        return cls(*(getattr(snapshot, item.name) for item in fields(GoalSnapshot)), *extra)

    def __post_init__(self) -> None:
        self.execution.validate_goal(self.goal.captured)
