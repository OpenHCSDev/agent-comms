"""What a unit did during its order, read from its own turn records."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.nominal.field_codec import ToolCallId
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.transcript.transcript_events import OrderCall, TurnDigest
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True)
class OrderWork(ToolResult):
    problem: str
    thread: ThreadIncarnation
    since: float
    calls: tuple[OrderCall, ...]

    @classmethod
    def read(
        cls, problem: str, thread: ThreadIncarnation, since: float, turns: tuple[TurnDigest, ...]
    ) -> OrderWork:
        """Each call once by tool_call_id, newest first."""
        calls: dict[ToolCallId, OrderCall] = {}
        for turn in reversed(turns):
            for event in turn.events:
                event.record_call(calls, since)
        return cls(problem, thread, since, tuple(reversed(calls.values())))
