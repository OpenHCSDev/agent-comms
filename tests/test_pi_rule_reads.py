"""Rule reads through the real saved Pi child, coding socket and owner turn."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_comms import backend
from agent_comms.instruction_deck import CommanderReader, FleetReader
from agent_comms.pi_events import CompactionEnd
from agent_comms.pi_payloads import CompactionData, ReportedCompaction, ReportedFlag
from agent_comms.turn_context import CoordinationSegment, RuleReadingSegment
from agent_comms.thread_identity import RecordedThreadIncarnation
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
            parts = tuple(child.rule_reading.parts)
            marker = fixture.project / "allowed.txt"
            blocked = fixture.project / "blocked.txt"
            fixture.provider.plan = [
                ("bash", {"command": f"touch {blocked}"}),
                ("read", {"path": str(parts[0]), "limit": 1}),
                ("read", {"path": str(fixture.project / '.git/config')}),
                ("bash", {"command": f"touch {blocked}"}),
                *(("read", {"path": str(part)}) for part in parts),
                ("bash", {"command": f"printf read > {marker}"}),
            ]
            async with fixture.original_input(agent, session_id, "Read the rules, then write the marker.") as turn:
                context = await persistent.context_for_input(turn.thread, turn.context)
                assert sum(isinstance(segment, RuleReadingSegment) for segment in context.segments) == 1
                await turn.prepare_native()
                await turn.stream()
            assert not blocked.exists()
            assert marker.read_text() == "read"
            child = persistent.child_state.child
            assert child.parts_read == set(parts)
            assert not child.pending_part_reads
            assert RuleReadingSegment in child.received_context
            assert CoordinationSegment in child.received_context
            results = [message for request in fixture.provider.requests for message in request["messages"]
                       if message["role"] == "tool"]
            denied = [message for message in results if message["tool_call_id"] in {"read-proof-1", "read-proof-2", "read-proof-3", "read-proof-4"}]
            assert denied and all(all(str(part) in message["content"] for part in parts) for message in denied)

            # Actual second owner input reuses the child and drops both once-per-context segments.
            before = len(fixture.provider.requests)
            async with fixture.original_input(agent, session_id, "Reply once without repeating the read order.") as turn:
                context = await persistent.context_for_input(turn.thread, turn.context)
                for kind in (RuleReadingSegment, CoordinationSegment):
                    assert kind not in tuple(type(segment) for segment in context.segments), (
                        f"Repeated {kind.__name__}\n"
                        f"old: {child.received_context[kind]!r}\n"
                        f"new: {next(segment.provenance for segment in context.segments if type(segment) is kind)!r}\n"
                        f"current: {persistent.child_state.current}; "
                        f"same session: {turn.thread.runtime.read_key == persistent.child_state.identity.session_file}; "
                        f"resume: {turn.thread.project_resume_due}; "
                        f"same cwd: {child.key[0].cwd == turn.thread.worktree.path}"
                    )
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
                order = next(segment for segment in context.segments if isinstance(segment, RuleReadingSegment))
                assert all(str(part) in order.text() for part in child.rule_reading.parts)
                await turn.prepare_native()
                await turn.stream()
            assert not blocked.exists()


@pytest.mark.parametrize("reader", (FleetReader, CommanderReader))
def test_prepared_rules_are_the_original_packaged_files(reader):
    prepared = reader.prepare()
    assert tuple(prepared) == tuple(source.resolve() for source in reader.sources())
    assert all(text == path.read_text(encoding="utf-8", newline="")
               for path, text in prepared.items())


def test_rule_reading_revision_covers_frozen_source_text_and_header() -> None:
    owner = RecordedThreadIncarnation("reader", 1.0)
    path = Path("/packaged/rule.md")
    parts = {path: "original source"}
    original = RuleReadingSegment.for_parts(owner, parts, "original header")
    assert original.provenance == RuleReadingSegment.for_parts(
        owner, parts, "original header").provenance
    parts[path] = "changed source at the same path"
    assert original.parts[path] == "original source"
    assert original.provenance != RuleReadingSegment.for_parts(
        owner, parts, "original header").provenance
    assert original.provenance != RuleReadingSegment.for_parts(
        owner, original.parts, "changed header").provenance


@pytest.mark.asyncio
async def test_a_cold_child_receives_the_order_in_its_first_actual_provider_request(
        tmp_path, native_runtime_package):
    async with native_backend_fixture(
        tmp_path, package=native_runtime_package, provider_type=RuleReadProvider,
    ) as fixture:
        await fixture.author_history()
        async with fixture.open_owner(native_options=()) as (agent, session_id):
            persistent = agent.turns.persistent_backends[session_id]
            await persistent.close_idle()
            assert not persistent.available and not fixture.provider.requests
            async with fixture.original_input(agent, session_id, "Report the received rule order.") as turn:
                await turn.prepare_native()
                assert not persistent.child_state.retained
                child = persistent.child_state.child
                fixture.children.append(child)
                await turn.stream()
            sent = json.dumps(fixture.provider.requests[0]["messages"])
            assert "Original saved question" in sent and "Original saved answer" in sent
            assert "Read each of these" in sent
            assert "FUCK YOUR COMPACTION" in sent
            assert all(str(path) in sent for path in child.rule_reading.parts)
