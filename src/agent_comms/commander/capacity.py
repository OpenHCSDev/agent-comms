"""One commander load report for native tools and the commander's CLI."""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..comms import Comms
from ..field_codec import projected
from ..tool_result import ToolResult
from ..turn_digests import OrderEditCountRead


@dataclass(frozen=True)
class Load(ToolResult):
    """Order and edit debt. Forking relieves it only by what forks clear: each verdict, order
    or answer a fork records lowers the debt itself, never a division by how many forks exist."""
    owed: int
    timers_up: int
    unjudged: int

    @classmethod
    def of(cls, comms: Comms, commander: str) -> Load:
        snapshot, todos, now = comms.registry.snapshot(), comms.goals.todos, time.time()
        owner = snapshot.require(commander)
        held = todos.held_for_commander(owner.incarnation)
        owed = tuple(t for t in held if t.pending)
        unjudged = sum(
            read.read() for read in OrderEditCountRead.for_orders(comms.root, snapshot, held)
        )
        return cls(len(owed), sum(bool(t.timer_up(now)) for t in owed), unjudged)

    @projected(view="wire")
    def total(self) -> float:
        return self.owed + 2 * self.timers_up + self.unjudged / 10

    def __str__(self) -> str:
        return (f'load {self.total:.0f} ({self.owed} open orders, {self.timers_up} review timers up, '
                f'{self.unjudged} unjudged edits)')


def main(a, ctx):
    print(Load.of(ctx.comms, ctx.commander))
