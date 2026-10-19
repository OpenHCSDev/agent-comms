"""Wake the commander once a minute on the next unit, and on every standing rule broken right now."""
import time
from datetime import datetime

from . import judge, order
from ..tools import CommsStandingRulesTool, StandingRulesRead
from ..todos import Todo
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()

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
            edits = judge.Edit.unjudged(order.COMMS, order.ME)
            waiting = sum(edit.thread == unit for edit in edits)
            print(f"ROTATE {datetime.now():%H:%M} {unit}: {orders}; unjudged {waiting} -> read its git state and newest turn, judge, push or hunt", flush=True)
        for line in broken_rules(since):
            print(f"  RULE {line}", flush=True)
        since = now
        time.sleep(60)
