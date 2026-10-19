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

from ..comms import Comms
from .config import CommanderConfig

PER_COMMANDER = 6
BEAT_AGE = 10 * 60


def live_forks(forks) -> list[str]:
    os.makedirs(forks, exist_ok=True)
    return [n[:-5] for n in os.listdir(forks) if n.endswith('.beat') and time.time() - os.path.getmtime(f'{forks}/{n}') < BEAT_AGE]


def beat(forks, name: str):
    os.makedirs(forks, exist_ok=True)
    with open(f'{forks}/{name}.beat', 'a'):
        os.utime(f'{forks}/{name}.beat')


@dataclass(frozen=True)
class Load:
    """The commander load on one Comms: its open and overdue orders, its unjudged edits, live forks."""
    owed: int
    overdue: int
    unjudged: int
    forks: tuple[str, ...]

    @classmethod
    def of(cls, comms: Comms, commander: str, forks) -> Load:
        from ..tools import CommsUnjudgedTool
        actor, now = comms.registry.require(commander).incarnation, time.time()
        owed = [t for t in comms.goals.todos.held_for_commander(actor) if t.pending]
        return cls(len(owed), sum(t.overdue(now) for t in owed),
                   CommsUnjudgedTool.count(comms, commander), tuple(live_forks(forks)))

    @property
    def total(self) -> float:
        return self.owed + 2 * self.overdue + self.unjudged / 10

    @property
    def needed(self) -> int:
        return math.ceil(self.total / PER_COMMANDER) - 1

    @property
    def short(self) -> bool:
        """Live forks do not cover the load: the commander must fork now."""
        return len(self.forks) < self.needed

    def __str__(self) -> str:
        return (f'load {self.total:.0f} ({self.owed} open orders, {self.overdue} overdue, '
                f'{self.unjudged} unjudged edits) needs {self.needed} live fork(s), '
                f'{len(self.forks)} live {list(self.forks)}; fork a slice now (each fork passes --fork NAME to order)')

    def require(self) -> None:
        if self.short:
            sys.exit(f'REFUSED, nothing recorded: overloaded: {self}.')


def main(a, commander):
    load = Load.of(commander.comms, commander.commander, commander.state / 'forks')
    print(load if load.short else f'covered: load {load.total:.0f}, forks {list(load.forks)}')
