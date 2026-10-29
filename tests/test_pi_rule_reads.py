"""Rule reads through the real saved Pi child, coding socket and owner turn."""

from __future__ import annotations

import json

import pytest

from agent_comms import backend
from agent_comms.instruction_deck import DeckParts, FleetReader
from agent_comms.pi_events import CompactionEnd
from agent_comms.pi_payloads import CompactionData, ReportedCompaction, ReportedFlag
from agent_comms.turn_context import CoordinationSegment, RuleReadingSegment
from compaction_loopback import LoopbackProvider
from native_backend_fixture import native_backend_fixture


class RuleReadProvider(LoopbackProvider):
    """Only the provider is controlled; Pi and Core execute every tool normally."""

    def __init__(self, **options):
        super().__init__(**options)
        self.plan = []
        self.number = 0
        self.on_answer = None

    def response_chunks(self):
        if not self.plan:
            yield {"content": self.text}, None
            yield {}, "stop"
            return
        name, arguments = self.plan.pop(0)
        self.number += 1
        yield {"tool_calls": [{"index": 0, "id": f"read-proof-{self.number}",
               "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}, None
        yield {}, "tool_calls"

    async def after_chunk(self):
        if not self.plan and self.on_answer is not None:
            action, self.on_answer = self.on_answer, None
            await action()


@pytest.mark.asyncio
async def test_saved_pi_requires_full_reads_and_rearms_on_compaction(tmp_path, native_runtime_package):
    async with native_backend_fixture(
        tmp_path, package=native_runtime_package, provider_type=RuleReadProvider,
    ) as fixture:
        async with fixture.open_owner(native_options=()) as (agent, session_id):
            persistent = agent.turns.persistent_backends[session_id]
            child = persistent.child_state.child
            parts = child.rule_parts
            marker = fixture.project / "allowed.txt"
            blocked = fixture.project / "blocked.txt"
            fixture.provider.plan = [
                ("bash", {"command": f"touch {blocked}"}),
                ("read", {"path": str(parts[0]), "limit": 1}),
                ("bash", {"command": f"touch {blocked}"}),
                *(("read", {"path": str(part)}) for part in parts),
                ("bash", {"command": f"printf read > {marker}"}),
            ]
            async with fixture.original_input(agent, session_id, "Read the rules, then write the marker.") as turn:
                assert isinstance(turn.context.segments[0], RuleReadingSegment)
                await turn.prepare_native()
                await turn.stream()
            assert not blocked.exists()
            assert marker.read_text() == "read"
            child = persistent.child_state.child
            assert child.parts_read == set(parts)
            assert RuleReadingSegment in child.received_context
            assert CoordinationSegment in child.received_context
            results = [message for request in fixture.provider.requests for message in request["messages"]
                       if message["role"] == "tool"]
            denied = [message for message in results if message["tool_call_id"] in {"read-proof-1", "read-proof-3"}]
            assert denied and all(all(str(part) in message["content"] for part in parts) for message in denied)

            # Actual second owner input reuses the child and drops both once-per-context segments.
            before = len(fixture.provider.requests)
            async with fixture.original_input(agent, session_id, "Reply once without repeating the read order.") as turn:
                context = await persistent.context_for_input(turn.thread, turn.context)
                assert not any(isinstance(segment, (RuleReadingSegment, CoordinationSegment)) for segment in context.segments)
                await turn.prepare_native()
                await turn.stream()
            assert len(fixture.provider.requests) == before + 1
            assert persistent.child_state.child is child

            # Drive the native protocol event into the actual active TurnSession, not a fake
            # watchdog/session. This isolates Core's CompactionEnd contract from summarization.
            async def compacted():
                native_turn = next(session for session in backend.TurnSession.active.values()
                                   if session.native is child)
                event = CompactionEnd(
                    aborted=ReportedFlag(False),
                    result=ReportedCompaction(CompactionData(summary="controlled compaction")),
                )
                async for _ in event.apply(native_turn):
                    pass
                assert not child.received_context and not child.parts_read

            fixture.provider.on_answer = compacted
            async with fixture.original_input(agent, session_id, "Exercise the compaction event.") as turn:
                await turn.prepare_native()
                await turn.stream()
            assert not child.received_context and not child.parts_read
            fixture.provider.plan = [("bash", {"command": f"touch {blocked}"})]
            async with fixture.original_input(agent, session_id, "After compaction, read before using bash.") as turn:
                context = await persistent.context_for_input(turn.thread, turn.context)
                assert isinstance(context.segments[0], RuleReadingSegment)
                assert all(str(part) in context.segments[0].text() for part in child.rule_parts)
                await turn.prepare_native()
                await turn.stream()
            assert not blocked.exists()


def test_shared_parts_fit_one_full_read():
    chunks = tuple(DeckParts.chunks(FleetReader.sources("")))
    assert "".join(chunks) == FleetReader.sources("")
    assert all(len(chunk.encode("utf-8")) <= DeckParts.part_bytes for chunk in chunks)
    assert all(len(chunk.splitlines()) <= DeckParts.part_lines for chunk in chunks)
