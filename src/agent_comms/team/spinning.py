"""Held orders whose unit keeps calling tools while its branch changes no source."""

from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.nominal.field_codec import OrderId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import HeldBranch, HeldBranchRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_digests import OrderToolCallCountRead, TurnsSince


@dataclass(frozen=True)
class SpinningTeammate(HeldBranchRule, declared_name="spinning_unit"):
    """Activity without a source change is a reportable condition, not progress. A commit is
    not a source change: the empty first commit every draft PR starts with moves HEAD and
    changes nothing (2026-10-25: unit-signal made 92 calls on P681 behind one)."""

    problem: str
    holder: ThreadIncarnation
    call_count: int = field(compare=False)

    calls: ClassVar[int] = 40

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        """Only a presence that may be active can spin; a stopped unit's old calls are history."""
        found = []
        for held in branches:
            source = held.source
            if not source.presence.may_be_active or held.difference.changes_source:
                continue
            count = OrderToolCallCountRead(
                source.comms.root, source.thread,
                TurnsSince(held.todo.order.checked().at.recorded_at("A held order")), held.todo,
            ).read()
            if count >= cls.calls:
                found.append(cls(held.problem, held.holder, count))
        return tuple(found)

    def line(self) -> str:
        return (f"{self.holder.name} made {self.call_count} calls on {self.problem} and changed 0 src"
                " lines. is it working, or just burning tokens? -> send the exact shape")


@dataclass(frozen=True)
class StaleUncommittedEdits(HeldBranchRule):
    """A held order's selected edits have stopped moving without becoming a commit."""

    problem: str
    order: OrderId
    holder: ThreadIncarnation
    edited_at: float
    now: float = field(compare=False)

    hours: ClassVar[int] = 2

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(
            cls(held.problem, held.todo.order.checked().id, held.holder, edited, held.source.now)
            for held in branches
            for progress in held.source.frame.pulls.progress(
                (held.todo,), held.source.comms.registry).values()
            for edited in progress.uncommitted_before(held.source.now - cls.hours * 3600)
        )

    def line(self) -> str:
        edited = RecordedEventTime(self.edited_at)
        return (f"COMMIT {self.holder.name} {self.problem}: uncommitted edits untouched since"
                f" {edited.clock()} ({edited.age(self.now)}). work nobody can see isn't progress."
                " -> commit and push it, or say why it stays local")
