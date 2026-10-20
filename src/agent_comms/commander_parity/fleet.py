"""Live board, branch and work comparisons against their current consumers."""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from types import CodeType, FrameType

from pydantic import JsonValue

from ..commander.judge import Edit
from ..commander.order import BoardVerb, Commander, hhmm
from ..field_codec import FieldCodec
from ..native_tool_call import CodingCall
from ..tool_caller import ThreadCaller
from ..tools import (
    CommsOrderWorkTool,
    CommsThreadsTool,
    CommsTodoListTool,
    CommsUnjudgedTool,
    CommsWorkBranchesTool,
    TodoList,
    UnjudgedEdits,
)
from ..transcript_events import ToolStartTranscript
from ..turn_digests import TurnsSince
from ..work_branches import WorkBranches
from . import ParityComparison, UnverifiedParity


@dataclass(frozen=True)
class ReadComparison(ParityComparison):
    """Compare complete projected facts, not a count that can hide different records."""

    owner: str
    current: JsonValue
    native: JsonValue

    @property
    def passed(self) -> bool:
        return self.current == self.native

    def describe(self) -> str:
        return f"owner={self.owner}; current_native_equal={self.passed}"


def observe[T](
    code: CodeType,
    result_type: type[T],
    current: Callable[[], JsonValue],
    native: Callable[[T], JsonValue],
    owner: str,
) -> ParityComparison:
    """Observe the real native return used by the current path without replacing either.

    A second read of a changing fleet is not a parity comparison of the first read.
    The profile hook only records returns and restores any existing observer.
    """
    results: list[T] = []
    previous = sys.getprofile()

    def returned(frame: FrameType, event: str, value: object) -> None:
        if event == "return" and frame.f_code is code and isinstance(value, result_type):
            results.append(value)
        if previous:
            previous(frame, event, value)

    sys.setprofile(returned)
    try:
        rendered = current()
    finally:
        sys.setprofile(previous)
    if len(results) != 1:
        return UnverifiedParity(
            f"{owner}: expected one live native return, observed {len(results)}"
        )
    return ReadComparison(owner, rendered, native(results[0]))


@dataclass(frozen=True)
class BoardFacts:
    problem: str
    owner_file: str
    holder: str
    order: str
    due: str
    progress: str
    delivered: bool
    strikes: int

    @classmethod
    def rendered(cls, board: str) -> tuple[BoardFacts, ...]:
        rows = []
        for line in board.splitlines():
            if re.match(r"^\| P[1-9][0-9]*:", line):
                values = tuple(part.strip() for part in line.split("|")[1:-1])
                if len(values) != 9:
                    raise ValueError("The current board has an ambiguous row")
                problem, owner_file, holder, order, due, progress, delivered, strikes, _ = values
                rows.append(
                    cls(
                        problem,
                        owner_file,
                        "" if holder.startswith("unassigned:") else holder,
                        order,
                        due,
                        progress,
                        delivered != "no",
                        int(strikes),
                    )
                )
        return tuple(rows)

    @classmethod
    def native_rows(cls, listed: TodoList) -> tuple[BoardFacts, ...]:
        return tuple(
            cls(
                f"{todo.id}: {todo.text}",
                todo.order.owner.to_wire(),
                todo.holder,
                todo.order.id,
                hhmm(todo.deadline),
                listed.progress[todo.id].line if todo.id in listed.progress else "-",
                not todo.pending,
                todo.strike_count,
            )
            for todo in sorted(listed.todos, key=lambda item: int(item.id[1:]))
        )


class BoardComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = Commander.read(Commander.path())
        fleet = CommsThreadsTool().apply(context.comms, ThreadCaller(context.commander))
        names = {record.thread.name for record in fleet.threads}

        def current() -> JsonValue:
            output = io.StringIO()
            with redirect_stdout(output):
                BoardVerb().run(context)
            return FieldCodec.encode(BoardFacts.rendered(output.getvalue()))

        def native(listed: TodoList) -> JsonValue:
            if any(todo.holder and todo.holder not in names for todo in listed.todos):
                raise ValueError("A board holder is absent from the live native fleet")
            return FieldCodec.encode(BoardFacts.native_rows(listed))

        return observe(
            CommsTodoListTool.apply.__code__,
            TodoList,
            current,
            native,
            "CommsTodoListTool + CommsThreadsTool / Commander BoardVerb",
        )


class BranchComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = Commander.read(Commander.path())
        current: list[JsonValue] = []
        native: list[JsonValue] = []
        for repo in context.repos:
            current.append(
                FieldCodec.encode(
                    WorkBranches(repo.checkout).read(context.comms.registry.snapshot())
                )
            )
            reply = CommsWorkBranchesTool(
                checkout=str(repo.checkout),
                unlinked_only=False,
            ).apply(context.comms, ThreadCaller(context.commander))
            native.append(FieldCodec.encode(reply.branches))
        return ReadComparison(
            "CommsWorkBranchesTool / current Git and forge branch reader",
            current,
            native,
        )


class OrderWorkComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = Commander.read(Commander.path())
        current: list[JsonValue] = []
        native: list[JsonValue] = []
        for todo in context.held_orders():
            since = todo.order.at.recorded_at("An order")
            turns = context.comms.transcripts.turns(todo.holder, TurnsSince(since))
            calls: dict[str, str] = {}
            for turn in reversed(turns.turns):
                for event in turn.events:
                    if isinstance(event, ToolStartTranscript) and not event.before(since):
                        call = CodingCall.decode(
                            event.tool_call_id, event.tool_name, event.raw_input
                        )
                        calls[event.tool_call_id] = call.digest()
            current.append(FieldCodec.encode((todo.id, tuple(reversed(calls.items())))))
            reply = CommsOrderWorkTool(problem=todo.id).apply(
                context.comms,
                ThreadCaller(context.commander),
            )
            native.append(
                FieldCodec.encode(
                    (
                        todo.id,
                        tuple((call.tool_call_id, call.digest) for call in reply.calls),
                    )
                )
            )
        return ReadComparison(
            "CommsOrderWorkTool / tool_watch CodingCall digests from live source turns",
            current,
            native,
        )


class UnjudgedComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = Commander.read(Commander.path())
        return observe(
            CommsUnjudgedTool.apply.__code__,
            UnjudgedEdits,
            lambda: FieldCodec.encode(Edit.unjudged(context.comms, context.commander)),
            lambda reply: FieldCodec.encode(reply.edits),
            "CommsUnjudgedTool / judge.Edit.unjudged",
        )
