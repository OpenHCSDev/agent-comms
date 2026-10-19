"""Commander load and forks (HARD RULE: as soon as the commander is overwhelmed it forks; a structural reflex).

Load is measured from the files the fleet already writes, never from how the commander feels:
each open order counts 1, each overdue order 2 more, and every 10 unjudged edits 1. One commander
carries PER_COMMANDER load. Forks register by passing `--fork NAME` to order, which touches
forks/NAME.beat; a beat younger than BEAT_AGE is a live fork. While load needs more forks than are
live, order refuses the main commander's actions and rotate prints FORK NOW, so the only
way forward is forking a slice.
"""
from __future__ import annotations

import math, os, sys, time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import CommanderConfig

if TYPE_CHECKING:
    from ..comms import Comms

FORKS = f'{CommanderConfig.installed().state}/forks'
PER_COMMANDER = 6
BEAT_AGE = 10 * 60


def live_forks() -> list[str]:
    os.makedirs(FORKS, exist_ok=True)
    return [n[:-5] for n in os.listdir(FORKS) if n.endswith('.beat') and time.time() - os.path.getmtime(f'{FORKS}/{n}') < BEAT_AGE]


def beat(name: str):
    os.makedirs(FORKS, exist_ok=True)
    with open(f'{FORKS}/{name}.beat', 'a'):
        os.utime(f'{FORKS}/{name}.beat')


@dataclass(frozen=True)
class Load:
    """The commander load on one Comms: its open and overdue orders and the edits it has not judged."""
    owed: int
    overdue: int
    unjudged: int

    @classmethod
    def of(cls, comms: Comms, commander: str) -> Load:
        from . import judge
        actor = comms.registry.require(commander).incarnation
        owed = [t for t in comms.goals.todos.held_for_commander(actor) if t.pending]
        return cls(len(owed), sum(t.late_since(time.time()) is not None for t in owed),
                   len(judge.Edit.unjudged(comms, commander)))

    @property
    def total(self) -> float:
        return self.owed + 2 * self.overdue + self.unjudged / 10

    def short(self) -> str | None:
        """Why the commander must fork now, or None when live forks cover the load."""
        needed = math.ceil(self.total / PER_COMMANDER) - 1
        forks = live_forks()
        if len(forks) < needed:
            return (f'load {self.total:.0f} ({self.owed} open orders, {self.overdue} overdue, '
                    f'{self.unjudged} unjudged edits) needs {needed} live fork(s), {len(forks)} live {forks}; '
                    f'fork a slice now (each fork passes --fork NAME to order)')
        return None

    def require(self) -> None:
        if why := self.short():
            sys.exit(f'REFUSED, nothing recorded: overloaded: {why}.')


def main(a):
    from . import order
    load = Load.of(order.COMMS, order.ME)
    print(load.short() or f'covered: load {load.total:.0f}, forks {live_forks()}')
