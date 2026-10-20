from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from agent_comms import todos
from agent_comms.change_report import FileLine
from agent_comms.commander.config import Repo
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec, NullMember, boundary_decoder
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import ThreadIncarnation

RETIRED = ("deadlines.json", "strikes.jsonl", "sent.jsonl", "hunts.jsonl")
SOURCES = ("problems.json", *RETIRED)
TOOLS = ("order.py", "judge.py", "rotate.py", "tool_watch.py", "shift_after_window.py")


@dataclass(frozen=True)
class RtsProblem:
    at: float
    text: str
    where: FileLine
    held_by: str = ""


@dataclass(frozen=True)
class RtsSent:
    at: float
    id: str
    text: str
    to: tuple[str, ...]


@dataclass(frozen=True)
class RtsStrike:
    at: float
    task: str | NullMember
    unit: str
    why: str
    caught: str = ""

    def recorded(self, actor: ThreadIncarnation, owner: ThreadIncarnation) -> todos.StrikeTodo:
        previous = todos.Assignment.of(owner, actor, f"rts-strike:{self.task}:{self.at}")
        return todos.StrikeTodo(1, actor, previous, self.why, RecordedEventTime(self.at))


@dataclass(frozen=True)
class RtsOrder:
    due: str
    msg: str
    at: float
    fact: str
    facts: tuple[str, ...]
    check: tuple[str, str, int]
    disk: bool
    works: str | NullMember
    started: bool = False
    merged: str | NullMember = NullMember()
    no_first_call: bool = False

    @boundary_decoder
    def declared(self, body: str) -> todos.PendingProblemOrder | todos.MergedProblemOrder:
        place = re.search(r"^Owner:\s*(\S+):(\d+)\s*$", body, re.M)
        report = re.search(r"\bReport:\s*([^\n]+)", body)
        if place is None or report is None or not re.search(r"^Banned:", body, re.M):
            raise ValueError(f"{self.facts} {self.msg}: the order needs Owner, Banned and Report")
        directory, command, expected = self.check
        check = OrderCheck.parse(f"{directory} :: {command} -> {expected}")
        if check is None:
            raise ValueError(f"{self.facts}: the order needs a check")
        run: todos.OrderRun = todos.MechanicalOrderRun()
        if isinstance(self.works, str):
            run = todos.ApplicationOrderRun(self.works)
        order = todos.PendingProblemOrder(
            id=self.msg,
            owner=FileLine(place[1], int(place[2])),
            check=check,
            edits=tuple(re.findall(r"^\s*\d+[.)] (.+?)\s*$", body, re.M)),
            banned=tuple(
                part.strip()
                for line in re.findall(r"^Banned:(.*)$", body, re.M)
                for part in line.split(";")
                if part.strip()
            ),
            report=report[1],
            at=RecordedEventTime(self.at),
            first_call_deadline=RecordedEventTime(self.at + 120),
            first_call=todos.ObservedFirstCall() if self.started else todos.AwaitingFirstCall(),
            run=run,
        )
        if isinstance(self.merged, str):
            return todos.MergedProblemOrder(original=order, sha=self.merged)
        return order


@boundary_decoder
def rts_hunt(line: bytes) -> todos.Hunt:
    values = json.loads(line)
    sent = values.pop("sent")
    values.update(
        at=FieldCodec.encode(RecordedEventTime(values.pop("at"))),
        kind="clean" if sent is None else "ordered",
    )
    return FieldCodec.decode(todos.Hunt, values if sent is None else {**values, "sent": sent})


def lines[T](data: bytes, kind: type[T]) -> list[T]:
    return [FieldCodec.decode(kind, json.loads(line)) for line in data.splitlines() if line]


def carry(rts: Path, root: Path, apply: bool, retire: bool) -> None:
    source = {rts / name: (rts / name).read_bytes() for name in SOURCES}
    bodies = {sent.id: sent.text for sent in lines(source[rts / "sent.jsonl"], RtsSent)}
    orders = [
        (unit, row, row.declared(bodies[row.msg]))
        for unit, rows in FieldCodec.decode(
            dict[str, tuple[RtsOrder, ...]], json.loads(source[rts / "deadlines.json"])
        ).items()
        for row in rows
    ]
    selected = {pid: (unit, row, order) for unit, row, order in orders for pid in row.facts}
    if len(selected) != sum(len(row.facts) for _, row, _ in orders):
        raise ValueError("A problem has two open RTS orders; split it first")
    problems = FieldCodec.decode(dict[str, RtsProblem], json.loads(source[rts / "problems.json"]))
    problems = {pid: problems[pid] for pid in selected}
    strikes = lines(source[rts / "strikes.jsonl"], RtsStrike)
    if apply:
        comms = Comms(root)
        actor = comms.registry.require("claude-code").incarnation
        with todos.TodoStore(root).writing() as db:
            existing = {todo.id: todo for todo in todos.Todo.stream(db)}
            for pid, (unit, row, order) in selected.items():
                owner = comms.registry.require(unit).incarnation
                proposed = todos.Assignment.of(owner, actor, "rts-order:" + row.msg)
                due = RecordedEventTime(row.at).at_clock(row.due)
                if pid in existing:
                    todo = existing[pid]
                    todo.require_commander(actor)
                    todo.state.require_open(todo)
                else:
                    print(f"{pid} order {row.msg}", file=sys.stderr, flush=True)
                    base = order.original if isinstance(order, todos.MergedProblemOrder) else order
                    repo = Repo.of(Path(base.owner.file).parent)
                    text = problems[pid].text
                    todo = todos.Todo.ordered(pid, repo.github, text, actor, proposed, due, order)
                todo = todos.AssignTodo(todo.revision, proposed, due, order, todo.goal).apply(todo)
                history = tuple(
                    strike.recorded(actor, comms.registry.require(strike.unit).incarnation)
                    for strike in strikes
                    if strike.task == pid
                )
                todo = replace(todo, strikes=history, revision=todo.revision + 1)
                if not todo.sent_in(row.msg):
                    todo = todos.OrderMessage(todo.revision, actor, row.msg).apply(todo)
                todo.upsert(db)
            for index, line in enumerate(source[rts / "hunts.jsonl"].splitlines()):
                todos.HuntRecord(f"rts-hunt:{index}", actor, rts_hunt(line)).upsert(db)
            if any(path.read_bytes() != data for path, data in source.items()):
                raise ValueError(f"RTS changed during the import; {root} is unchanged")
        if retire:
            for name in TOOLS + RETIRED:
                (rts / name).rename(rts / f"{name}.retired-p707")
    print(json.dumps({"orders": len(selected), "strikes": len(strikes)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rts-dir", dest="rts", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retire-rts", dest="retire", action="store_true")
    carry(**vars(parser.parse_args()))
