"""Wake the commander once a minute on the next unit, and on every standing rule broken right now."""
import time
from datetime import datetime

from . import judge, order
from ..tools import CommsStandingRulesTool, StandingRulesRead
from ..todos import Todo


def broken_rules(ctx, after=0.0):
    """Every standing rule that broke after ``after``, as Core's StandingRule family derives it."""
    read: StandingRulesRead = ctx.call(CommsStandingRulesTool(after=after))
    return read.broken


def owed_orders(ctx: order.Commander) -> dict[str, list[Todo]]:
    """Each unit's open orders."""
    owed: dict[str, list[Todo]] = {}
    for todo in ctx.held_orders():
        owed.setdefault(todo.held().owner, []).append(todo)
    return owed


def main(a, ctx):
    turn, since = 0, 0.0  # since: the previous wake; a rule that stays broken is said once
    while True:
        now = time.time()
        owed = owed_orders(ctx)
        units = sorted(owed)
        if units:
            unit = units[turn % len(units)]
            turn += 1
            read = ctx.progress()
            orders = ', '.join(f"{t.id} due {t.deadline.clock()} ({read[t.id].line})"
                               for t in owed[unit])
            edits = judge.Edit.unjudged(ctx.comms, ctx.commander)
            waiting = sum(edit.thread == unit for edit in edits)
            print(f"ROTATE {datetime.now():%H:%M} {unit}: {orders}; unjudged {waiting} -> read its git state and newest turn, judge, push or hunt", flush=True)
        for line in broken_rules(ctx, since):
            print(f"  RULE {line}", flush=True)
        since = now
        time.sleep(60)
