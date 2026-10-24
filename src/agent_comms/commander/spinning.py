"""Held orders whose unit keeps calling tools without moving its checkout head."""

from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.standing_rules import HeldBranch, HeldBranchRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_digests import OrderToolCallCountRead, TurnsSince


@dataclass(frozen=True)
class SpinningUnit(HeldBranchRule):
    """Activity without a new commit is a reportable condition, not progress."""

    problem: str
    holder: ThreadIncarnation
    call_count: int = field(compare=False)
    elapsed_minutes: float = field(compare=False)
    last_commit: str

    calls: ClassVar[int] = 30
    minutes: ClassVar[int] = 20

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        """Only a presence that may be active can spin; a stopped unit's old calls are history."""
        found = []
        for held in branches:
            source = held.source
            if not source.presence.may_be_active:
                continue
            since = max(held.difference.moved_at, held.todo.order.checked().at.recorded_at("A held order"))
            elapsed = (source.now - since) / 60
            if elapsed < cls.minutes:
                continue
            count = OrderToolCallCountRead(
                source.comms.root, source.thread, TurnsSince(since), held.todo,
            ).read()
            if count >= cls.calls:
                found.append(cls(held.problem, held.holder, count, elapsed, held.difference.head))
        return tuple(found)

    def line(self) -> str:
        return (f"SPINNING UNIT: {self.problem} {self.holder.name}: {self.call_count} tool calls "
                f"in {self.elapsed_minutes:.1f} min without moving HEAD {self.last_commit} "
                "-> inspect the repeated work and change the approach")
