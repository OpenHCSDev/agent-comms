"""Temporary RTS -> Todo carry; remove after the authorized live carry.

Run with the installed Python, an explicit copied RTS directory and copied bus:
    python tools/cutover/carry_rts.py --rts-dir COPY --root COPIED_BUS [--apply]

Preflight is deliberately atomic: missing identities/evidence stop ALL writes.
An RTS edit's random ID is NOT a Core tool-call ID. This tool never invents a
request, assignment generation, checked delivery, artifact or historical time.
Only problems named by open orders, and their strikes, are carried.
Unordered problems and other strikes stay in RTS; their counts are reported.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import Todo, TodoStore


@dataclass(frozen=True)
class RtsOrder:
    due: str
    msg: str
    at: float
    fact: str
    facts: tuple[str, ...]
    check: tuple[str, str, int]
    disk: bool
    works: str | None = None
    started: bool = False

    def deadline(self) -> RecordedEventTime:
        # This is the original due_at() interpretation of the old HH:MM format,
        # not today's date and not the time this carry happens to run.
        hour, minute = map(int, self.due.split(':'))
        sent = time.localtime(self.at)
        at = time.mktime((sent.tm_year, sent.tm_mon, sent.tm_mday,
                          hour, minute, 0, 0, 0, -1))
        return RecordedEventTime(at + 86400 if at < self.at - 60 else at)


@dataclass(frozen=True)
class RtsProblem:
    text: str
    where: str
    at: float
    held_by: str | None = None


@dataclass(frozen=True)
class RtsStrike:
    unit: str
    why: str
    at: float
    task: str | None
    caught: str | None = None


def read_json(path: Path, kind):
    return FieldCodec.decode(kind, FieldCodec.parse(path.read_text()))


def read_lines(path: Path, kind):
    text = path.read_text()
    if text and not text.endswith('\n'):
        raise ValueError(f'{path}: incomplete final row; no records were carried')
    return tuple(FieldCodec.decode(kind, FieldCodec.parse(line))
                 for line in text.splitlines() if line.strip())


def carry(rts: Path, root: Path, apply: bool) -> int:
    # Decode the external format once, before consulting or opening a writer.
    deadlines = read_json(rts / 'deadlines.json', dict[str, tuple[RtsOrder, ...]])
    ordered_problems = {pid for orders in deadlines.values() for old in orders for pid in old.facts}
    problem_records = FieldCodec.parse((rts / 'problems.json').read_text())
    problems = {pid: FieldCodec.decode(RtsProblem, problem_records[pid])
                for pid in ordered_problems}
    recorded_strikes = read_lines(rts / 'strikes.jsonl', RtsStrike)
    strikes = tuple(strike for strike in recorded_strikes if strike.task in problems)
    archived_strikes = len(recorded_strikes) - len(strikes)
    comms = Comms(root)
    actor = comms.registry.require('claude-code').incarnation
    store = TodoStore(root)
    existing = {row.id: row for row in store.history_for_commander(actor)}
    prepared: dict[str, Todo] = {}
    failures = Counter()
    examples: dict[str, list[str]] = defaultdict(list)

    def missing(reason: str, identity: str):
        failures[reason] += 1
        examples[reason].append(identity)

    active = set()
    for unit, orders in deadlines.items():
        for old in orders:
            for pid in old.facts:
                if pid in active:
                    missing('RTS names more than one active order for this problem', pid)
                    continue
                active.add(pid)
                current = existing.get(pid)
                if current is None:
                    missing('order has no recorded Core assignment/request generation', pid)
                    continue
                if (current.assignment is None or current.holder != unit
                        or current.order.id != old.msg):
                    missing('RTS and Core name different current assignments/orders', pid)
                    continue
                if current.order.at.recorded_at('An order') != old.at:
                    missing('order send times disagree', pid)
                    continue
                due = old.deadline()
                if current.deadline != due:
                    prepared[pid] = replace(current, deadline=due,
                                            revision=current.revision + 1)

    for pid, old in problems.items():
        current = existing.get(pid)
        if current is None:
            missing('problem has no complete order representable by Core Todo', pid)
        elif current.text != old.text:
            missing('problem text differs between RTS and Core', pid)
        elif not current.state.open:
            missing('RTS problem remains open but Core has it closed', pid)
        elif pid not in active and current.assignment is not None:
            missing('RTS has no order but Core still assigns this problem', pid)

    for old in strikes:
        current = existing.get(old.task)
        matching = () if current is None else tuple(
            strike for strike in current.strikes
            if strike.previous.owner == old.unit
            and strike.at.recorded_at('A strike') == old.at
            and strike.reason in (old.why, f'{old.why} (caught by {old.caught})'))
        if not matching:
            missing('strike needs its original assignment, not the current thread goal', old.task)

    report = {
        'rts': str(rts), 'root': str(root),
        'source': {'orders': sum(map(len, deadlines.values())), 'problems': len(problems),
                   'strikes': len(strikes)},
        'archive': (f'{len(problem_records) - len(problems)} unordered problems and '
                    f'{archived_strikes} strikes outside ordered tasks stay in the RTS files'),
        'archived_problems': len(problem_records) - len(problems),
        'archived_strikes': archived_strikes,
        'carried': {'orders': 0, 'problems': 0, 'strikes': 0},
        'core_rows': len(existing), 'prepared_identified_orders': sorted(prepared),
        'blockers': dict(failures), 'examples': dict(examples),
        'todo_writes': 0,
    }
    if failures:
        report['result'] = 'REFUSED: no Todo writes; source facts need owner model support'
        print(json.dumps(report, indent=2))
        return 2
    if apply:
        # One transaction; preserve Core-only rows, goals, inputs and history.
        with store.writing() as db:
            for pid, row in prepared.items():
                store._current(db, pid).require_result(existing[pid])
                row.upsert(db)
            for pid, row in prepared.items():
                store._current(db, pid).require_result(row)
        report['todo_writes'] = len(prepared)
        report['carried'] = dict(report['source'])
    report['result'] = 'carried' if apply else 'ready'
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rts-dir', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    return carry(args.rts_dir, args.root, args.apply)


if __name__ == '__main__':
    raise SystemExit(main())
