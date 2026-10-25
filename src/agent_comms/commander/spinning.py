"""Held orders whose unit keeps calling tools while its branch changes no source."""

from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.standing_rules import HeldBranch, HeldBranchRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_digests import OrderToolCallCountRead, TurnsSince


@dataclass(frozen=True)
class SpinningUnit(HeldBranchRule):
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
        return (f"SPINNING {self.holder.name}: {self.call_count} calls, 0 src lines since order "
                f"{self.problem} -> commander sends the exact shape")
