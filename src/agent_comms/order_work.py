"""What a unit did during its order, read from its own turn records."""

from __future__ import annotations

from dataclasses import dataclass

from .thread_identity import ThreadIncarnation
from .tool_result import ToolResult
from .transcript_events import OrderCall, TurnDigest


@dataclass(frozen=True)
class OrderWork(ToolResult):
    problem: str
    thread: ThreadIncarnation
    since: float
    calls: tuple[OrderCall, ...]

    @classmethod
    def read(cls, problem: str, thread: ThreadIncarnation, since: float,
             turns: tuple[TurnDigest, ...]) -> OrderWork:
        """Each call once by tool_call_id, newest first."""
        calls: dict[str, OrderCall] = {}
        for turn in reversed(turns):
            for event in turn.events:
                event.record_call(calls, since)
        return cls(problem, thread, since, tuple(reversed(calls.values())))
