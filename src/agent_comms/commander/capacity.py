"""One commander load report for native tools and the commander's CLI."""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..comms import Comms
from ..field_codec import projected
from ..tool_result import ToolResult


@dataclass(frozen=True)
class Load(ToolResult):
    """Held work and ready heads, with receiving children that command orders."""
    owed: int
    timers_up: int
    heads_waiting: int
    forks: tuple[str, ...]

    @classmethod
    def of(cls, comms: Comms, commander: str) -> Load:
        snapshot, todos, now = comms.registry.snapshot(), comms.goals.todos, time.time()
        owner = snapshot.require(commander)
        held = todos.held_for_commander(owner.incarnation)
        owed = tuple(t for t in held if t.pending)
        forks = tuple(sorted(
            thread.name for thread in snapshot.threads.values()
            if thread.parent == owner.name and snapshot.status(thread.name).receiving
            and todos.held_for_commander(thread.incarnation)
        ))
        from .standing import Unjudged

        return cls(len(owed), sum(bool(t.timer_up(now)) for t in owed),
                   len(Unjudged.violations(comms, commander)), forks)

    @projected(view="wire")
    def total(self) -> float:
        return self.owed + 2 * self.timers_up + self.heads_waiting

    def __str__(self) -> str:
        return (f'load {self.total:.0f} ({self.owed} open orders, {self.timers_up} review timers up, '
                f'{self.heads_waiting} unreviewed pull requests), {len(self.forks)} live forks {list(self.forks)}')


def main(a, ctx):
    print(Load.of(ctx.comms, ctx.commander))
