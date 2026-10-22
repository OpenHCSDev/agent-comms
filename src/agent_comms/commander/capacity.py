"""One commander load report for native tools and the commander's CLI."""
from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from ..comms import Comms
from ..field_codec import projected
from ..tool_result import ToolResult
from ..todos import PullRequests
from ..turn_digests import OrderEditCountRead
from ..ui_model.unit_state import UnitState


@dataclass(frozen=True)
class Load(ToolResult):
    """Order and edit debt, and the working forks that share it: a fork reading the debt now is
    another reader of it, so the debt each thread carries is the total over the commander and
    its working forks. An idle fork reads nothing; it counts once it works again or retires."""
    owed: int
    timers_up: int
    unjudged: int
    forks: tuple[str, ...]

    @classmethod
    def of(cls, comms: Comms, commander: str) -> Load:
        return cls.from_states(comms, commander, UnitState.current(comms, commander, PullRequests()))

    @classmethod
    def from_states(cls, comms: Comms, commander: str, observed: Sequence[UnitState]) -> Load:
        snapshot, todos, now = comms.registry.snapshot(), comms.goals.todos, time.time()
        owner = snapshot.require(commander)
        held = todos.held_for_commander(owner.incarnation)
        owed = tuple(t for t in held if t.pending)
        forks = tuple(sorted(
            state.unit.name for state in observed
            if state.source.thread.parent.named(owner.name) and state.source.working
        ))
        unjudged = sum(
            read.read() for read in OrderEditCountRead.for_orders(comms.root, snapshot, held)
        )
        return cls(len(owed), sum(bool(t.timer_up(now)) for t in owed), unjudged, forks)

    @projected(view="wire")
    def total(self) -> float:
        return self.owed + 2 * self.timers_up + self.unjudged / 10

    @projected(view="wire")
    def share(self) -> float:
        """The debt per reader: forking is how a commander relieves its load."""
        return self.total / (1 + len(self.forks))

    def __str__(self) -> str:
        return (f'load {self.total:.0f} ({self.owed} open orders, {self.timers_up} review timers up, '
                f'{self.unjudged} unjudged edits), {len(self.forks)} working forks {list(self.forks)},'
                f' {self.share:.0f} per reader')


def main(a, ctx):
    print(Load.of(ctx.comms, ctx.commander))
