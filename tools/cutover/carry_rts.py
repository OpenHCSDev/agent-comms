"""Temporary move of open RTS orders into a copied Core root.

The root contains registry/Todo copies and order-messages.jsonl: exact original
bus rows selected by the RTS message ids. Never run against the live root.
Core's order actions choose assignments and goal generations; this tool does
not write Todo rows or infer generations from old goals.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.commander.config import Repo
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, SetGoalAction
from agent_comms.goals import OrderCheck, RequestGoalOrder
from agent_comms.messages import Message
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (ApplicationOrderRun, AwaitingFirstCall, MechanicalOrderRun,
    ObservedFirstCall, PendingProblemOrder, StrikeTodo, TodoStore)


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

    def order(self, message: Message) -> PendingProblemOrder:
        body = message.body
        place = re.search(r'^Owner:\s*(\S+):(\d+)\s*$', body, re.M)
        report = re.search(r'^Report:\s*(.+)$', body, re.M)
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
        raise ValueError(f'{path}: incomplete final row')
    return tuple(FieldCodec.decode(kind, FieldCodec.parse(line))
                 for line in text.splitlines() if line.strip())


def carry(rts: Path, root: Path, apply: bool) -> int:
    deadlines = read_json(rts / 'deadlines.json', dict[str, tuple[RtsOrder, ...]])
    records = FieldCodec.parse((rts / 'problems.json').read_text())
    strikes = read_lines(rts / 'strikes.jsonl', RtsStrike)
    messages = {}
    for line in (root / 'order-messages.jsonl').read_text().splitlines():
        message = Message.from_committed_wire(FieldCodec.parse(line))
        messages[message.message_id] = message
    ordered = {}
    blockers = {}
    for unit, orders in deadlines.items():
        for old in orders:
            for pid in old.facts:
                if pid in ordered:
                    blockers[pid] = 'two open orders name this problem'
                ordered[pid] = (unit, old)
    selected = {pid: FieldCodec.decode(RtsProblem, records[pid]) for pid in ordered}
    parsed_orders = {old.msg: old.order(messages[old.msg])
                     for orders in deadlines.values() for old in orders}
    attached = tuple(strike for strike in strikes
                     if strike.task in ordered and strike.unit == ordered[strike.task][0])
    report = {'orders_issued': {'create': 0, 'assign': 0}, 'strikes_attached': 0,
              'strikes_left_as_history': len(strikes) - len(attached),
              'unordered_problems': len(records) - len(selected),
              'source_orders': sum(map(len, deadlines.values())),
              'source_ordered_problems': len(ordered),
              'source_first_call_exemptions': [old.msg for orders in deadlines.values()
                                               for old in orders if old.no_first_call],
              'blockers': blockers}
    if blockers or not apply:
        print(json.dumps(report, indent=2))
        return 2 if blockers else 0
    comms = Comms(root)
    actor = comms.registry.require('claude-code')
    requests = tuple(goal.id for goal in actor.goal.active()
                     if isinstance(goal.order, RequestGoalOrder))
    if not requests:
        comms.goals.update_goal(actor.name, SetGoalAction(
            text='Move open RTS orders into this copied Core root', order=RequestGoalOrder()))
    request = comms.registry.require(actor.name).goal.require().id
    store = TodoStore(root)
    existing = {row.id: row for row in store.history_for_commander(actor.incarnation)}
    for pid, (unit, old) in ordered.items():
        try:
            message = messages[old.msg]
            if message.sender != actor.name:
                raise ValueError(f'{old.msg}: order was not sent by the commander')
            order = parsed_orders[old.msg]
            common = dict(problem=pid, order=order, request=request,
                          deadline=old.deadline(), commander=actor.name)
            if pid in existing:
                action = OrderGoalAction(**common, expected_revision=store.get(pid).revision)
                kind = 'assign'
            else:
                action = CreateProblemGoalAction(**common,
                    repo=Repo.of(Path(order.owner.file).parent).github, text=selected[pid].text)
                kind = 'create'
            comms.goals.update_goal(unit, action)
            report['orders_issued'][kind] += 1
            for old_strike in attached:
                if old_strike.task != pid:
                    continue
                current = store.get(pid)
                comms.goals.change_problem(pid, StrikeTodo(
                    current.revision, actor.incarnation, current.held(), old_strike.why,
                    RecordedEventTime(old_strike.at)), actor.name)
                report['strikes_attached'] += 1
        except (ValueError, KeyError) as error:
            blockers[pid] = str(error)
    print(json.dumps(report, indent=2))
    return 2 if blockers else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rts-dir', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    return carry(args.rts_dir, args.root, args.apply)


if __name__ == '__main__':
    raise SystemExit(main())
