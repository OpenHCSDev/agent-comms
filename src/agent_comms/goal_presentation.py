"""Goal execution is a read of the decisions, wait, ledger and live owner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar, Self

from .field_codec import NullMember, RecordMember, SubclassUnion, WireValue
from .goal_failure_observation import FailedTurnEvidence
from .goal_states import PauseSource
from .goals import Goal, GoalCheckpoint, GoalDeclaration
from .lifecycle_capability import Active
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
    def from_wire(cls, data: dict[str, object]) -> GoalWaitTarget:
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
    @property
    @abstractmethod
    def label(self) -> str: ...

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


class ControlledExecution(BoundGoalExecution, declared_name=None):
    """An execution the owner moves on with one control; the others offer none."""

    toggle_label: ClassVar[str] = "Pause"

    @property
    @abstractmethod
    def toggle(self) -> type[GoalAction]: ...


class ActiveExecution(ControlledExecution, declared_name=None):
    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import PausedGoalAction
        return PausedGoalAction


@dataclass(frozen=True)
class RunnableExecution(ActiveExecution):
    label = "Active"


@dataclass(frozen=True)
class RunningExecution(ActiveExecution):
    label = "Running"


@dataclass(frozen=True)
class CompletedExecution(BoundGoalExecution):
    label = "Completed"


@dataclass(frozen=True)
class PausedExecution(WaitingExecution, ControlledExecution):
    source: PauseSource
    label = "Paused"
    toggle_label = "Resume"

    @property
    def toggle(self) -> type[GoalAction]:
        from .goal_actions import ActiveGoalAction
        return ActiveGoalAction


@dataclass(frozen=True)
class StandbyExecution(ActiveExecution, WaitingExecution):
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

    def details_heading(self, heading: str) -> str:
        return f"{heading} · {self.summary}"

    def standby(self) -> bool:
        return True

    def waiting_for(self, goal: Goal) -> bool:
        return goal.id == self.goal_id and bool(goal.state.items_with(Active, goal))


class BlockedExecution(WaitingExecution, ControlledExecution, declared_name=None):
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
                f"{self.reason} -> inspect the failed work; never replay")


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
