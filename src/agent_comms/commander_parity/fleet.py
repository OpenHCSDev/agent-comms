"""Live board, branch and work comparisons against their current consumers."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from configparser import ConfigParser
from contextlib import redirect_stdout
from dataclasses import dataclass
from sys import monitoring
from types import CodeType

from pydantic import JsonValue

from ..commander.judge import Edit
from ..commander.order import BoardVerb, Commander, hhmm
from ..field_codec import FieldCodec
from ..native_entries import NativeEntry
from ..native_tool_call import CodingCall
from ..tool_caller import ThreadCaller
from ..tool_declaration import EditCall
from ..tools import (
    CommsOrderWorkTool,
    CommsThreadsTool,
    CommsTodoListTool,
    CommsUnjudgedTool,
    CommsWorkBranchesTool,
    TodoList,
    UnjudgedEdits,
)
from ..work_branches import WorkBranches
from . import ParityComparison, UnverifiedParity


@dataclass(frozen=True)
class ReadComparison(ParityComparison):
    """Compare projected facts, not a count that can hide different records."""

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
    current: Callable[[], object],
    native: Callable[[T], object],
    owner: str,
) -> ParityComparison:
    """Observe the real native return used by the current path without replacing either.

    A second read of a changing fleet is not a comparison of the first read.
    Only this function's return event is enabled; other profilers are untouched.
    """
    results: list[T] = []
    tool = monitoring.PROFILER_ID
    if monitoring.get_tool(tool):
        return UnverifiedParity(f"{owner}: the Python profiler slot is already owned")

    def returned(returned_code: CodeType, offset: int, value: T) -> None:
        results.append(value)

    monitoring.use_tool_id(tool, __name__)
    try:
        monitoring.register_callback(tool, monitoring.events.PY_RETURN, returned)
        monitoring.set_local_events(tool, code, monitoring.events.PY_RETURN)
        rendered = current()
    finally:
        monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.PY_RETURN, None)
        monitoring.free_tool_id(tool)
    if len(results) != 1:
        return UnverifiedParity(
            f"{owner}: expected one live native return, observed {len(results)}"
        )
    return ReadComparison(owner, FieldCodec.encode(rendered), FieldCodec.encode(native(results[0])))


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
                        ConfigParser.BOOLEAN_STATES[delivered.partition(",")[0]],
                        int(strikes),
                    )
                )
        return tuple(rows)

    @classmethod
    def native_rows(cls, listed: TodoList) -> tuple[BoardFacts, ...]:
        return tuple(
            cls(
                f"{todo.id}: {todo.text}",
                todo.order.checked().owner.to_wire(),
                todo.holder or "",
                todo.order.checked().id,
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

        def current() -> tuple[BoardFacts, ...]:
            output = io.StringIO()
            with redirect_stdout(output):
                BoardVerb().run(context)
            return BoardFacts.rendered(output.getvalue())

        def native(listed: TodoList) -> tuple[BoardFacts, ...]:
            if any(todo.holder and todo.holder not in names for todo in listed.todos):
                raise ValueError("A board holder is absent from the live native fleet")
            return BoardFacts.native_rows(listed)

        return observe(
            CommsTodoListTool.apply.__code__,
            current,
            native,
            "CommsTodoListTool + CommsThreadsTool / Commander BoardVerb",
        )


class BranchComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = Commander.read(Commander.path())
        if not context.repos:
            return UnverifiedParity("CommsWorkBranchesTool has no configured repository to invoke")
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
        held = context.held_orders()
        if not held:
            return UnverifiedParity("CommsOrderWorkTool has no live held problem to invoke")
        current: list[JsonValue] = []
        native: list[JsonValue] = []
        for todo in held:
            reply = CommsOrderWorkTool(problem=todo.id).apply(
                context.comms,
                ThreadCaller(context.commander),
            )
            since = todo.order.checked().at.recorded_at("An order")
            calls: dict[str, str] = {}
            session = context.comms.registry.require(todo.held().owner).session_file
            if not session:
                return UnverifiedParity("Current tool_watch has no native session for this order")
            with open(session, "rb") as source:
                for line in source:
                    if not line.endswith(b"\n"):
                        return UnverifiedParity(
                            "Current tool_watch source ends in an incomplete record"
                        )
                    entry = NativeEntry.read(line)
                    if entry.timestamp.at.timestamp() >= since:
                        for message in entry.messages:
                            if message.assistant:
                                for pi_call in message.tool_calls:
                                    call = CodingCall.decode(
                                        pi_call.id, pi_call.name, pi_call.arguments
                                    )
                                    calls[pi_call.id] = call.digest()
            current.append(FieldCodec.encode((todo.id, tuple(reversed(calls.items())))))
            native.append(
                FieldCodec.encode(
                    (
                        todo.id,
                        tuple((call.tool_call_id, call.digest) for call in reply.calls),
                    )
                )
            )
        return ReadComparison(
            "CommsOrderWorkTool / actual tool_watch NativeEntry and CodingCall digests",
            current,
            native,
        )


class UnjudgedComparison:
    @classmethod
    def read(cls) -> ParityComparison:
        context = Commander.read(Commander.path())

        def native(reply: UnjudgedEdits) -> tuple[EditCall, ...]:
            return reply.edits

        return observe(
            CommsUnjudgedTool.apply.__code__,
            lambda: Edit.unjudged(context.comms, context.commander),
            native,
            "CommsUnjudgedTool / judge.Edit.unjudged",
        )
