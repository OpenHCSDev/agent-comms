"""One-shot RTS order transfer. Run under a quiesced writer; remove after installation.

Only Core's real record types and TodoStore write the destination. This does not
publish goals, send messages, signal owners or launch a provider. --retire-rts
removes the old executable entrypoints after the transferred rows are verified.
Unordered findings have no order/owner/ceiling and are not fabricated as orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.commander.config import Repo
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.todos import (ApplicationOrderRun, Assignment, AwaitingFirstCall,
    MechanicalOrderRun, NoGoalReference, ObservedFirstCall, OrderMessage,
    PendingProblemOrder, ReleaseTodo, StrikeReason, StrikeTodo, TodoStore, WithdrawTodo,
    Todo, OpenTodoState, UnorderedProblemOrder, LocatedProblemFinding, MergedProblemOrder,
    HuntRecord, CleanHunt, OrderedHunt)


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
    no_first_call: bool = False

    def deadline(self) -> RecordedEventTime:
        hour, minute = map(int, self.due.split(':'))
        sent = time.localtime(self.at)
        at = time.mktime((sent.tm_year, sent.tm_mon, sent.tm_mday,
                          hour, minute, 0, 0, 0, -1))
        return RecordedEventTime(at + 86400 if at < self.at - 60 else at)

    def order(self, body: str) -> PendingProblemOrder:
        place = re.search(r'^Owner:\s*(\S+):(\d+)\s*$', body, re.M)
        report = re.search(r'\bReport:\s*([^\n]+)', body)
        if place is None or report is None:
            raise ValueError(f'{self.msg}: original order needs Owner and Report lines')
        return PendingProblemOrder(
            id=self.msg, owner=FileLine(place[1], int(place[2])),
            check=OrderCheck.parse(f'{self.check[0]} :: {self.check[1]} -> {self.check[2]}'),
            edits=tuple(re.findall(r'^\s*\d+[.)] (.+?)\s*$', body, re.M)),
            banned=tuple(part.strip() for line in re.findall(r'^Banned:(.*)$', body, re.M)
                         for part in line.split(';') if part.strip()),
            report=report[1], at=RecordedEventTime(self.at),
            first_call_deadline=RecordedEventTime(self.at + 120),
            first_call=ObservedFirstCall() if self.started else AwaitingFirstCall(),
            run=ApplicationOrderRun(self.works) if self.works else MechanicalOrderRun())


def carry(rts: Path, root: Path, apply: bool, retire: bool = False) -> int:
    names = ('deadlines.json', 'problems.json', 'strikes.jsonl', 'sent.jsonl', 'hunts.jsonl')
    source = {name: (rts / name).read_bytes() for name in names}
    deadlines = json.loads(source['deadlines.json'])
    problems = json.loads(source['problems.json'])
    sent = [json.loads(line) for line in source['sent.jsonl'].splitlines() if line]
    messages = {item['id']: item for item in sent}
    strikes = [json.loads(line) for line in source['strikes.jsonl'].splitlines() if line]
    selected = {}
    merged = {}
    for unit, orders in deadlines.items():
        for raw in orders:
            values = dict(raw)
            commit = values.pop('merged', '')
            old = FieldCodec.decode(RtsOrder, values)
            parsed = old.order(messages[old.msg]['text'])
            for pid in old.facts:
                if pid in selected:
                    raise ValueError(f'{pid}: two open RTS orders')
                selected[pid] = unit, old, parsed
                if commit:
                    merged[pid] = commit
    missing = sorted(selected.keys() - problems.keys())
    if missing:
        print(json.dumps({'orders_missing_problem_rows': missing}, indent=2))
        raise ValueError('The commander must restore the missing problem rows before transfer')
    finding_repos = {}
    for pid in sorted(problems.keys() - selected.keys()):
        location = FileLine.from_wire(problems[pid]['where'])
        try:
            finding_repos[pid] = Repo.of(Path(location.path).parent).github
        except subprocess.CalledProcessError as error:
            raise ValueError(f'{pid}: cannot read repository from recorded path {location.path}') from error
    comms = Comms(root)
    actor = comms.registry.require('claude-code')
    store = TodoStore(root)
    rows = []
    for pid, (unit, old, order) in selected.items():
        owner = comms.registry.require(unit)
        repo = Repo.of(Path(order.check.directory)).github
        history = []
        for strike in strikes:
            if strike.get('task') != pid:
                continue
            struck = comms.registry.require(strike['unit'])
            # RTS strikes name a task and unit, not an order or goal generation.
            # Keep that original event identity; never guess an earlier order.
            assignment = Assignment(struck.name, struck.incarnation.created_at,
                actor.name, actor.incarnation.created_at, f"rts-strike:{pid}:{strike['at']}")
            history.append(StrikeTodo(1, actor.incarnation, assignment,
                strike['why'], RecordedEventTime(strike['at'])))
        rows.append((pid, repo, problems[pid]['text'], owner, old, order, tuple(history)))
    report = {'source_sha256': {name: hashlib.sha256(data).hexdigest()
                               for name, data in source.items()},
              'orders': len(rows), 'strikes': sum(len(row[-1]) for row in rows),
              'unordered_findings': len(problems.keys() - selected.keys()),
              'orders_missing_problem_rows': sorted(selected.keys() - problems.keys()),
              'already_merged': merged, 'board_orders': []}
    if not apply:
        print(json.dumps(report, indent=2))
        return 0
    existing = {todo.id: todo for todo in store.history_for_commander(actor.incarnation)}
    for pid, current in existing.items():
        if current.assignment is not None and pid not in selected:
            reason = 'This old order is absent from the current open RTS orders.'
            if pid in problems:
                store.apply(pid, ReleaseTodo(current.revision, actor.incarnation,
                    current.held(), StrikeReason(reason)))
            else:
                store.apply(pid, WithdrawTodo(current.revision, actor.incarnation,
                    current.held(), reason))
    for pid, repo, text, owner, old, order, history in rows:
        if pid in existing:
            current = store.get(pid)
            if current.order.id != old.msg:
                current = store.assign(pid, expected_revision=current.revision,
                    owner=owner, parent=actor, generation='rts-order:' + old.msg,
                    goal=current.goal, deadline=old.deadline(), order=order)
        else:
            current = store.create(pid, repo, text, creator=actor, owner=owner,
                generation='rts-order:' + old.msg, deadline=old.deadline(), order=order,
                goal=NoGoalReference())
        if not current.sent_in(old.msg):
            current = store.apply(pid, OrderMessage(current.revision, actor.incarnation, old.msg))
        # Historical strikes include former holders; replaying StrikeTodo.apply would
        # wrongly require those holders to be the current assignment. Restore their
        # actual Core records, together, through the record's own SQLite codec.
        recorded_order = order.copy_as(MergedProblemOrder, commit=merged[pid]) if pid in merged else order
        restored = replace(current, order=recorded_order, strikes=history, revision=current.revision + 1)
        with store.writing() as db:
            restored.upsert(db)
        recorded = store.get(pid)
        if (recorded.holder != owner.name or recorded.deadline != old.deadline()
                or recorded.order.id != old.msg or recorded.strikes != history):
            raise ValueError(f'{pid}: transferred Core row differs')
        report['board_orders'].append(f'{pid} {recorded.holder} {old.due} {recorded.order.id}')
    for pid, repo in finding_repos.items():
        problem = problems[pid]
        location = FileLine.from_wire(problem['where'])
        finding = Todo(id=pid, repo=repo, text=problem['text'], creator=actor.name,
            creator_created=actor.created_at, state=OpenTodoState, revision=1,
            goal=NoGoalReference(), assignment=None,
            order=UnorderedProblemOrder(finding=LocatedProblemFinding(location)),
            deadline=UnrecordedEventTime())
        if pid in existing:
            previous = store.get(pid)
            finding = replace(finding, revision=previous.revision + 1,
                              strikes=previous.strikes, released=previous.released)
        with store.writing() as db:
            finding.upsert(db)
    hunts = [json.loads(line) for line in source['hunts.jsonl'].splitlines() if line]
    for index, raw in enumerate(hunts):
        fields = dict(where=FileLine.from_wire(raw['where']), why=raw['why'],
                      at=RecordedEventTime(raw['at']))
        hunt = OrderedHunt(**fields, sent=raw['sent']) if raw['sent'] else CleanHunt(**fields)
        # Restore historical records; do not rerun a historical source-file
        # existence check or pretend that an old order is still current.
        with store.writing() as db:
            HuntRecord(id=f'rts-hunt:{index}', commander=actor.incarnation, hunt=hunt).upsert(db)
    report['finding_repositories'] = finding_repos
    report['hunts'] = len(hunts)
    report['merged_without_delivery_claim'] = len(merged)
    if any((rts / name).read_bytes() != data for name, data in source.items()):
        raise ValueError('RTS changed during transfer; destination is not ready for installation')
    if retire:
        for name in ('order.py', 'judge.py', 'rotate.py', 'tool_watch.py', 'shift_after_window.py',
                     'deadlines.json', 'problems.json', 'strikes.jsonl', 'hunts.jsonl', 'sent.jsonl'):
            (rts / name).rename(rts / (name + '.retired-p707'))
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rts-dir', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--retire-rts', action='store_true')
    args = parser.parse_args()
    return carry(args.rts_dir, args.root, args.apply, args.retire_rts)


if __name__ == '__main__':
    raise SystemExit(main())
