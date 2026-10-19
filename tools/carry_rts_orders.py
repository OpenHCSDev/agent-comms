"""One-time import of the RTS orders, findings, strikes and hunts into Core's todo store.

Run once on a quiesced root during the install, then delete this file. Every write goes
through TodoStore on a staged copy that replaces the store in one SQLite backup. Nothing
here publishes goals, sends messages or launches a provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.commander.config import Repo
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec, boundary_decoder
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.todos import (ApplicationOrderRun, Assignment, AwaitingFirstCall,
    CheckedProblemOrder, Hunt, HuntRecord, LocatedProblemFinding, MechanicalOrderRun,
    MergedProblemOrder, NoGoalReference, ObservedFirstCall, OpenTodoState, OrderMessage,
    PendingProblemOrder, ReleaseTodo, StrikeReason, StrikeTodo, Todo, TodoStore,
    UnorderedProblemOrder, WithdrawTodo)

SOURCES = ('deadlines.json', 'problems.json', 'strikes.jsonl', 'sent.jsonl', 'hunts.jsonl')
TOOLS = ('order.py', 'judge.py', 'rotate.py', 'tool_watch.py', 'shift_after_window.py')


@dataclass(frozen=True)
class RtsProblem:
    at: float
    text: str
    where: FileLine
    held_by: str = ''


@dataclass(frozen=True)
class RtsSent:
    at: float
    id: str
    text: str
    to: str


@dataclass(frozen=True)
class RtsStrike:
    at: float
    task: str
    unit: str
    why: str
    caught: str = ''


@dataclass(frozen=True)
class RtsOrder:
    unit: str
    due: str
    msg: str
    at: float
    facts: tuple[str, ...]
    order: CheckedProblemOrder

    def deadline(self) -> RecordedEventTime:
        hour, minute = map(int, self.due.split(':'))
        sent = time.localtime(self.at)
        at = time.mktime((sent.tm_year, sent.tm_mon, sent.tm_mday, hour, minute, 0, 0, 0, -1))
        return RecordedEventTime(at + 86400 if at < self.at - 60 else at)


@boundary_decoder
def rts_order(unit: str, raw: dict[str, object], bodies: dict[str, str]) -> RtsOrder:
    """The RTS row spells a mechanical order as null works and a merge as an optional sha."""
    msg, at, works, merged = raw['msg'], raw['at'], raw['works'], raw.get('merged')
    body = bodies[msg]
    place = re.search(r'^Owner:\s*(\S+):(\d+)\s*$', body, re.M)
    report = re.search(r'\bReport:\s*([^\n]+)', body)
    if place is None or report is None or not re.search(r'^Banned:', body, re.M):
        raise ValueError(f'{raw["facts"]} {msg}: the order needs Owner, Banned and Report lines')
    directory, command, expected = raw['check']
    order = PendingProblemOrder(
        id=msg, owner=FileLine(place[1], int(place[2])),
        check=OrderCheck.parse(f'{directory} :: {command} -> {expected}'),
        edits=tuple(re.findall(r'^\s*\d+[.)] (.+?)\s*$', body, re.M)),
        banned=tuple(part.strip() for line in re.findall(r'^Banned:(.*)$', body, re.M)
                     for part in line.split(';') if part.strip()),
        report=report[1], at=RecordedEventTime(at), first_call_deadline=RecordedEventTime(at + 120),
        first_call=ObservedFirstCall() if raw.get('started') else AwaitingFirstCall(),
        run=MechanicalOrderRun() if works is None else ApplicationOrderRun(works))
    recorded = order if merged is None else order.copy_as(MergedProblemOrder, commit=merged)
    return RtsOrder(unit, raw['due'], msg, at, tuple(raw['facts']), recorded)


@boundary_decoder
def rts_hunt(line: bytes) -> Hunt:
    """RTS spells a clean hunt as a null "sent"; this is its one decode."""
    values = json.loads(line)
    sent = values.pop('sent')
    values.update(at=FieldCodec.encode(RecordedEventTime(values.pop('at'))),
                  kind='clean' if sent is None else 'ordered')
    return FieldCodec.decode(Hunt, values if sent is None else {**values, 'sent': sent})


def lines[T](data: bytes, kind: type[T]) -> list[T]:
    return [FieldCodec.decode(kind, json.loads(line)) for line in data.splitlines() if line]


def carry(rts: Path, root: Path, apply: bool, retire: bool) -> None:
    source = {name: (rts / name).read_bytes() for name in SOURCES}
    bodies = {sent.id: sent.text for sent in lines(source['sent.jsonl'], RtsSent)}
    orders = [rts_order(unit, raw, bodies) for unit, rows in
              json.loads(source['deadlines.json']).items() for raw in rows]
    selected = {pid: old for old in orders for pid in old.facts}
    if len(selected) != sum(len(old.facts) for old in orders):
        raise ValueError('A problem has two open RTS orders; split it first')
    problems = {pid: FieldCodec.decode(RtsProblem, raw)
                for pid, raw in json.loads(source['problems.json']).items()}
    if missing := sorted(selected.keys() - problems.keys()):
        raise ValueError(f'Restore the missing problem rows first: {missing}')
    strikes = lines(source['strikes.jsonl'], RtsStrike)
    repos, gone = {}, []
    for pid in sorted(problems.keys() - selected.keys()):
        try:
            repos[pid] = Repo.of(Path(problems[pid].where.file).parent).github
        except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
            gone.append(f'{pid} {problems[pid].where.file}')
    if gone:
        raise ValueError('Re-locate or drop the findings whose checkout is gone:\n' + '\n'.join(gone))
    report = {'source_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in source.items()},
              'orders': len(selected), 'unordered_findings': len(repos), 'strikes': len(strikes)}
    if not apply:
        print(json.dumps(report, indent=2))
        return
    comms = Comms(root)
    actor = comms.registry.require('claude-code')
    destination = TodoStore(root)
    staging = Path(tempfile.mkdtemp(prefix='p707-carry-', dir=root))
    store = TodoStore(staging)
    with sqlite3.connect(destination.path_in(root)) as live, sqlite3.connect(store.path_in(staging)) as staged:
        live.backup(staged)
    existing = {todo.id: todo for todo in store.history_for_commander(actor.incarnation)}
    for pid, current in existing.items():
        if current.assignment is not None and pid not in selected:
            reason = 'This old order is absent from the current open RTS orders.'
            store.apply(pid, ReleaseTodo(current.revision, actor.incarnation, current.held(),
                                         StrikeReason(reason)) if pid in problems else
                        WithdrawTodo(current.revision, actor.incarnation, current.held(), reason))
    for pid, old in selected.items():
        owner = comms.registry.require(old.unit)
        # RTS strikes name a task and unit, not an order; keep that event identity.
        history = tuple(StrikeTodo(1, actor.incarnation, Assignment(
            strike.unit, comms.registry.require(strike.unit).incarnation.created_at, actor.name,
            actor.incarnation.created_at, f'rts-strike:{pid}:{strike.at}'),
            strike.why, RecordedEventTime(strike.at)) for strike in strikes if strike.task == pid)
        generation = 'rts-order:' + old.msg
        if pid not in existing:
            current = store.create(pid, Repo.of(Path(old.order.check.directory)).github,
                                   problems[pid].text, creator=actor, owner=owner,
                                   generation=generation, deadline=old.deadline(),
                                   order=old.order, goal=NoGoalReference())
        elif existing[pid].order.id != old.msg:
            current = store.assign(pid, expected_revision=existing[pid].revision, owner=owner,
                                   parent=actor, generation=generation, goal=existing[pid].goal,
                                   deadline=old.deadline(), order=old.order)
        else:
            current = store.get(pid)
        if not current.sent_in(old.msg):
            current = store.apply(pid, OrderMessage(current.revision, actor.incarnation, old.msg))
        # Former holders' strikes cannot replay through StrikeTodo; restore the records.
        with store.writing() as db:
            replace(current, order=old.order, strikes=history, revision=current.revision + 1).upsert(db)
        recorded = store.get(pid)
        if (recorded.holder, recorded.deadline, recorded.order, recorded.strikes) != (
                owner.name, old.deadline(), old.order, history):
            raise ValueError(f'{pid}: the imported Core row differs from its RTS order')
    for pid, repo in repos.items():
        finding = Todo(id=pid, repo=repo, text=problems[pid].text, creator=actor.name,
                       creator_created=actor.created_at, state=OpenTodoState, revision=1,
                       goal=NoGoalReference(), assignment=None, deadline=UnrecordedEventTime(),
                       order=UnorderedProblemOrder(finding=LocatedProblemFinding(problems[pid].where)))
        if pid in existing:
            finding = replace(finding, revision=existing[pid].revision + 1,
                              strikes=existing[pid].strikes, released=existing[pid].released)
        with store.writing() as db:
            finding.upsert(db)
    with store.writing() as db:
        for index, line in enumerate(source['hunts.jsonl'].splitlines()):
            HuntRecord(id=f'rts-hunt:{index}', commander=actor.incarnation, hunt=rts_hunt(line)).upsert(db)
    if any((rts / name).read_bytes() != data for name, data in source.items()):
        raise ValueError(f'RTS changed during the import; {root} is unchanged, staged copy at {staging}')
    with sqlite3.connect(store.path_in(staging)) as staged, sqlite3.connect(destination.path_in(root)) as live:
        staged.backup(live)
    for path in staging.iterdir():
        path.unlink()
    staging.rmdir()
    for name in (TOOLS + SOURCES) * retire:
        (rts / name).rename(rts / f'{name}.retired-p707')
    print(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rts-dir', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--retire-rts', action='store_true')
    args = parser.parse_args()
    carry(args.rts_dir, args.root, args.apply, args.retire_rts)


if __name__ == '__main__':
    main()
