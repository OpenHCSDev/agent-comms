"""Wake the commander once a minute on the next unit, and on every standing rule broken right now."""
import time
from datetime import datetime

from ..todos import Todo
from ..tools import CommsStandingRulesTool, StandingRulesRead
from . import judge, order
from .context import CommanderContext


def broken_rules(commander, after=0.0):
    """Every standing rule that broke after ``after``, as Core's StandingRule family derives it."""
    read: StandingRulesRead = commander.call(CommsStandingRulesTool(after=after))
    return read.broken


def owed_orders(commander: CommanderContext) -> dict[str, list[Todo]]:
    """Each unit's open orders."""
    owed: dict[str, list[Todo]] = {}
    for todo in commander.held_orders():
        owed.setdefault(todo.held().owner, []).append(todo)
    return owed


def main(a, commander):
    turn, since = 0, 0.0  # since: the previous wake; a rule that stays broken is said once
    while True:
        now = time.time()
        owed = owed_orders(commander)
        units = sorted(owed)
        if units:
            unit = units[turn % len(units)]
            turn += 1
            read = commander.progress()
            orders = ', '.join(f"{t.id} due {order.hhmm(t.deadline)} ({read[t.order].line})"
                               for t in owed[unit])
            edits = judge.Edit.unjudged(commander.comms, commander.commander)
            waiting = sum(edit.thread == unit for edit in edits)
            print(f"ROTATE {datetime.now():%H:%M} {unit}: {orders}; unjudged {waiting} -> read its git state and newest turn, judge, push or hunt", flush=True)
        for line in broken_rules(commander, since):
            print(f"  RULE {line}", flush=True)
        since = now
        time.sleep(60)
