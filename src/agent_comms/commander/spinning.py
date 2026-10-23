"""Held orders whose unit keeps calling tools without moving its checkout head."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.standing_rules import UndatedStandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_digests import OrderToolCallCountRead, TurnsSince
from agent_comms.ui_model.unit_state import UnitState


@dataclass(frozen=True)
class SpinningUnit(UndatedStandingRule):
    """Activity without a new commit is a reportable condition, not progress."""

    problem: str
    holder: ThreadIncarnation
    call_count: int = field(compare=False)
    elapsed_minutes: float = field(compare=False)
    last_commit: str

    calls: ClassVar[int] = 30
    minutes: ClassVar[int] = 20

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]:
        found = []
        for state in observed:
            source = state.source
            if not source.todos:
                continue
            for holder in state.readable_units():
                branch = source.pulls.branch_difference(source.thread.project_path)
                for todo in source.todos:
                    since = max(branch.moved_at, todo.order.checked().at.recorded_at("A held order"))
                    elapsed = (source.now - since) / 60
                    if elapsed < cls.minutes:
                        continue
                    count = OrderToolCallCountRead(
                        comms.root, source.thread, TurnsSince(since), todo,
                    ).read()
                    if count >= cls.calls:
                        found.append(cls(todo.id, holder, count, elapsed, branch.head))
        return tuple(found)

    def line(self) -> str:
        return (f"SPINNING UNIT: {self.problem} {self.holder.name}: {self.call_count} tool calls "
                f"in {self.elapsed_minutes:.1f} min without moving HEAD {self.last_commit} "
                "-> inspect the repeated work and change the approach")
