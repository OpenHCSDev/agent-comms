"""Report commander load and live forks; no command refuses on capacity.

Each open order counts 1, each overdue order 2 more, and every 10 unjudged edits 1.
Forks register with `--fork NAME`; a heartbeat younger than BEAT_AGE is live.
"""
from __future__ import annotations

import os, time
from dataclasses import dataclass

from ..comms import Comms
from . import order
from .config import CommanderConfig

FORKS = f'{CommanderConfig.installed().state}/forks'
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
    """The commander load on one Comms: its open and overdue orders, its unjudged edits, live forks."""
    owed: int
    overdue: int
    unjudged: int
    forks: tuple[str, ...]

    @classmethod
    def of(cls, comms: Comms, commander: str) -> Load:
        from ..tools import CommsUnjudgedTool
        actor, now = comms.registry.require(commander).incarnation, time.time()
        owed = [t for t in comms.goals.todos.held_for_commander(actor) if t.pending]
        return cls(len(owed), sum(t.overdue(now) for t in owed),
                   CommsUnjudgedTool.count(comms, commander), tuple(live_forks()))

    @property
    def total(self) -> float:
        return self.owed + 2 * self.overdue + self.unjudged / 10

    def __str__(self) -> str:
        return (f'load {self.total:.0f} ({self.owed} open orders, {self.overdue} overdue, '
                f'{self.unjudged} unjudged edits), {len(self.forks)} live forks {list(self.forks)}')


def main(a):
    print(Load.of(order.COMMS, order.ME))
