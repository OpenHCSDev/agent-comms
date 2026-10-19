"""Wake the commander once a minute on the next unit, and on every standing rule broken right now."""
import time
from datetime import datetime

from . import judge, order, request
from ..tools import CommsStandingRulesTool, StandingRulesRead
from ..todos import Todo
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()

def unjudged(unit=None):
    edits = judge.Edit.unjudged(order.COMMS, order.ME)
    return sum(1 for edit in edits if unit in (None, edit.thread))


def broken_rules(after=0.0):
    """Every standing rule that broke after ``after``, as Core's StandingRule family derives it."""
    read: StandingRulesRead = order.call(CommsStandingRulesTool(after=after))
    return read.broken


def owed_orders() -> dict[str, list[Todo]]:
    """Each unit's open orders."""
    owed: dict[str, list[Todo]] = {}
    for todo in order.held_orders():
        owed.setdefault(todo.held().owner, []).append(todo)
    return owed


def detached(owed: dict[str, list[Todo]]) -> list[str]:
    """Each unit holding orders whose status reads nothing now, or whose thread is gone."""
    statuses = order.COMMS.registry.snapshot().statuses
    away = [u for u in sorted(owed) if u not in statuses or not statuses[u].receiving]
    unread = order.COMMS.bus.pending_counts_all(tuple(u for u in away if u in statuses))
    return [f"DETACHED {u}: {unread.get(u, 0)} unread, orders {', '.join(t.id for t in owed[u])}"
            " -> wake it or reassign" for u in away]


def main(a):
    turn, since = 0, 0.0  # since: the previous wake; a rule that stays broken is said once
    while True:
        now = time.time()
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
        for line in broken_rules(since):
            print(f"  RULE {line}", flush=True)
        since = now
        time.sleep(60)
