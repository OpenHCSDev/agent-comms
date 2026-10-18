"""Wake the commander once a minute on the next unit, and on every standing rule broken right now."""
import time
from datetime import datetime

from . import capacity, judge, order, request, rules
from ..standing_rules import StandingRule
from ..todos import Todo
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
ROOT = str(CONFIG.state)


def unjudged(unit=None):
    return sum(1 for e in judge.unjudged() if unit in (None, e['thread']))


def broken_rules(owed):
    """Every skill rule whose state is checkable from files, as plain lines."""
    lines = []
    seen = {line[9:].split(' ', 1)[0] for line in open(f'{ROOT}/activity.log', errors='replace').readlines()[-6000:]}
    # Parked threads hold standing owner decisions (blocked goals, frozen work); the commander is not a unit.
    idle = sorted(u for u in seen if u and not owed.get(u) and u not in CONFIG.parked | {CONFIG.commander})
    if idle:
        lines.append(f"IDLE UNITS with no order: {', '.join(idle)} -> give each the top unowned row now")
    if time.time() - order.last_hunt() > 900:
        lines.append('HUNT DUE: read a hot path and order the fix this wake')
    if why := capacity.short():
        lines.append(f'FORK NOW: {why}')
    if not order.COMMS.registry.snapshot().statuses[CONFIG.commander].receiving:
        lines.append('COMMS BRIDGE DOWN: the commander receives nothing -> re-arm the bridge Monitor this wake')
    if (total := unjudged()) > 30:
        lines.append(f'UNJUDGED {total} edits -> judge or fork a judge slice')
    if why := rules.stale():
        lines.append(f'RULES DUE ({why}) -> agent-comms-commander rules review and answer the drawn rules before any order')
    lines.extend(rule.line() for rule in StandingRule.broken(order.todos()))
    return lines


def owed_orders() -> dict[str, list[Todo]]:
    """Each unit's open orders."""
    owed: dict[str, list[Todo]] = {}
    for todo in order.open_todos():
        owed.setdefault(todo.assignment.owner, []).append(todo)
    return owed


def detached(owed: dict[str, list[Todo]]) -> list[str]:
    """Each unit holding orders whose status reads nothing now, or whose thread is gone."""
    statuses = order.COMMS.registry.snapshot().statuses
    away = [u for u in sorted(owed) if u not in statuses or not statuses[u].receiving]
    unread = order.COMMS.bus.pending_counts_all(tuple(u for u in away if u in statuses))
    return [f"DETACHED {u}: {unread.get(u, 0)} unread, orders {', '.join(t.id for t in owed[u])}"
            " -> wake it or reassign" for u in away]


def main(a):
    turn = 0
    while True:
        owed = owed_orders()
        units = sorted(owed)
        if units:
            unit = units[turn % len(units)]
            turn += 1
            read = order.progress()
            orders = ', '.join(f"{t.id} due {order.hhmm(t.deadline)} ({read[t.id].line})"
                               for t in owed[unit])
            print(f"ROTATE {datetime.now():%H:%M} {unit}: {orders}; unjudged {unjudged(unit)} -> read its git state and newest turn, judge, push or hunt", flush=True)
        for line in detached(owed):
            print(line, flush=True)
        for line in request.stalled():
            print(line, flush=True)
        for line in broken_rules(owed):
            print(f"  RULE {line}", flush=True)
        time.sleep(60)
