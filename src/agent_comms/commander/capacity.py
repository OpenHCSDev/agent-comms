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
    """Order and edit debt, with receiving children that themselves command orders."""
    owed: int
    overdue: int
    unjudged: int
    forks: tuple[str, ...]

    @classmethod
    def of(cls, comms: Comms, commander: str) -> Load:
        snapshot, todos, now = comms.registry.snapshot(), comms.goals.todos, time.time()
        owner = snapshot.require(commander)
        owed = tuple(t for t in todos.held_for_commander(owner.incarnation) if t.pending)
        forks = tuple(sorted(
            thread.name for thread in snapshot.threads.values()
            if thread.parent == owner.name and snapshot.statuses[thread.name].receiving
            and todos.held_for_commander(thread.incarnation)
        ))
        return cls(len(owed), sum(t.overdue(now) for t in owed),
                   OrderEditCountRead.count(comms, owner.name), forks)

    @projected(view="wire")
    def total(self) -> float:
        return self.owed + 2 * self.overdue + self.unjudged / 10

    def __str__(self) -> str:
        return (f'load {self.total:.0f} ({self.owed} open orders, {self.overdue} overdue, '
                f'{self.unjudged} unjudged edits), {len(self.forks)} live forks {list(self.forks)}')


def main(a, ctx):
    print(Load.of(ctx.comms, ctx.commander))
