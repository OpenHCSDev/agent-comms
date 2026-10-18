"""Commander load and forks (HARD RULE: as soon as the commander is overwhelmed it forks; a structural reflex).

Load is measured from the files the fleet already writes, never from how the commander feels:
each open order counts 1, each overdue order 2 more, and every 10 unjudged edits 1. One commander
carries PER_COMMANDER load. Forks register by passing `--fork NAME` to order, which touches
forks/NAME.beat; a beat younger than BEAT_AGE is a live fork. While load needs more forks than are
live, order refuses the main commander's actions and rotate prints FORK NOW, so the only
way forward is forking a slice.
"""
import math, os, sys, time

from .config import CommanderConfig

FORKS = f'{CommanderConfig.installed().state}/forks'
PER_COMMANDER = 6
BEAT_AGE = 10 * 60


def load() -> tuple[float, str]:
    from . import judge, order
    owed = [t for t in order.held_orders() if t.pending]
    overdue = sum(t.late_since(time.time()) is not None for t in owed)
    unjudged = len(judge.unjudged())
    return len(owed) + 2 * overdue + unjudged / 10, f'{len(owed)} open orders, {overdue} overdue, {unjudged} unjudged edits'


def live_forks() -> list[str]:
    os.makedirs(FORKS, exist_ok=True)
    return [n[:-5] for n in os.listdir(FORKS) if n.endswith('.beat') and time.time() - os.path.getmtime(f'{FORKS}/{n}') < BEAT_AGE]


def beat(name: str):
    os.makedirs(FORKS, exist_ok=True)
    with open(f'{FORKS}/{name}.beat', 'a'):
        os.utime(f'{FORKS}/{name}.beat')


def short() -> str | None:
    """Why the commander must fork now, or None when live forks cover the load."""
    total, parts = load()
    needed = math.ceil(total / PER_COMMANDER) - 1
    forks = live_forks()
    if len(forks) < needed:
        return (f'load {total:.0f} ({parts}) needs {needed} live fork(s), {len(forks)} live {forks}; '
                f'fork a slice now (each fork passes --fork NAME to order)')
    return None


def require():
    if why := short():
        sys.exit(f'REFUSED, nothing recorded: overloaded: {why}.')


def main(a):
    print(short() or f'covered: load {load()[0]:.0f}, forks {live_forks()}')
