"""Canonical original-bus batch capture and one-use native reservation."""
from conftest import publish_initial
from binding_reads import binding_rows

import asyncio
import json
import os
import sqlite3
from unittest import TestCase

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, pending_sealed_assignments
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.native_prompt_binding import bind_expected_prompt
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.private_send_stage import TriageNativeSend
from agent_comms.selected_participant import SelectedParticipant
from agent_comms.selected_turn import SelectedPrompt
from agent_comms.threads import Thread


@pytest.mark.asyncio
async def test_original_pending_wave_has_one_fenced_input_and_late_arrivals_stay_pending(tmp_path, monkeypatch, *, cursor_delivery):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    for name in ("sender", "receiver"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model='openai-codex/gpt-6-sol'))
    root_id = comms.messaging.initialize_private_initial_protocol()
    lookup = RecordedThreadIncarnation("fixture", comms.registry.require("receiver").created_at).lookup
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(lookup, "receiver", "receiver", committed=True)

        def send(body):
            message = publish_initial(comms, "sender", "#team", body)
            accept_delivery_cohort(comms.bus, root_id, message.seq, store)
            return message

        # Cross the canonical SQL page boundary; a full snapshot must not cap at
        # one page or an arbitrary number of pending candidates.
        originals = tuple(
            send(f"Original pending question {index}: " + "λ" * 512) for index in range(101)
        )
        snapshot = pending_sealed_assignments(store, lookup, "receiver")
        assert tuple(row.wire_seq for row in snapshot) == tuple(row.seq for row in originals)
        published = []
        async def publish_compaction(event):
            published.append(event)

        original_capture = SelectedParticipant.capture_sent_outcomes

        def capture_before_turn(selected_comms, owner, sources, assignment_read):
            assert not selected_comms.registry.require(owner.name).turn_record.read().executing
            return original_capture(selected_comms, owner, sources, assignment_read)

        def forbid_full_presentation(*_args, **_kwargs):
            raise AssertionError("selected outcome capture prepared a full thread presentation")

        monkeypatch.setattr(SelectedParticipant, "capture_sent_outcomes", capture_before_turn)
        monkeypatch.setattr(comms.views, "thread_views", forbid_full_presentation)
        monkeypatch.setattr(
            CoordinationStore, "observing",
            staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("claim path opened a second coordination reader")
            )),
        )
        blocker = sqlite3.connect(root / "coordination.sqlite3", isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")

        async def release_blocker():
            await asyncio.sleep(0.1)
            blocker.execute("ROLLBACK")
            blocker.close()

        release = asyncio.create_task(release_blocker())
        async with SelectedParticipant.select(
            comms,
            store,
            root_id,
            "receiver",
            0,
            on_compaction=publish_compaction,
            cursor_delivery=cursor_delivery,
        ) as selected:
            from agent_comms.agent_events import CompactionEnd, CompactionStart
            observations = (CompactionStart(), CompactionEnd(summary="Committed original summary"))
            for observation in observations:
                await selected.dispatch(observation)
            assert len(published) == 2
            assert all(actual is original for actual, original in zip(published, observations, strict=True))
            assert selected.batch.assignments == snapshot
            prompt = SelectedPrompt(selected).triage().text
            from agent_comms.wake_policy import WakePolicy

            # The original instruction is shared by the whole captured batch,
            # rather than multiplying mandatory context for every source row.
            assert prompt.count(WakePolicy.relevance_instruction().content) == 1
            # Mandatory original content belongs to native selected-model claim_scope,
            # not a Python-wide byte cap or the optional awareness resource bound.
            assert len(prompt.encode("utf-8")) > 32 * 1024
            assert all(json.dumps(original.body, ensure_ascii=True) in prompt for original in originals)
            late = send("Arrived after work-start capture")
            assert late.seq not in tuple(row.wire_seq for row in selected.batch.assignments)
            stage = TriageNativeSend(selected.batch.assignments)
            token_digest = "a" * 64
            input_id = stage.reserve(store, selected.identity, token_digest)
            bind_expected_prompt(
                store, input_id=input_id, stage=stage,
                owner=selected.owner.thread, generation=selected.identity.generation,
                prompt=prompt,
            )
            binding = binding_rows(store, (input_id,)).get(input_id)
            assert binding.input_id == input_id
            with store.session.read():
                assert len(NativeRuntimeInput.select(store.session._connection)) == 1
                assert stage.execution.source_assignment_ids(store.session._connection, input_id) == tuple(
                    row.assignment_id for row in snapshot
                )
                stage.require_claim(store)
            # Binding does not establish a live native result. All 101 members
            # are now uncertain/nonreplayable; the late original is the next wave.
            pending = pending_sealed_assignments(store, lookup, "receiver")
            assert tuple(row.wire_seq for row in pending) == (late.seq,)
            with TestCase().assertRaises(IdentityConflict):
                stage.reserve(store, selected.identity, token_digest)
        await release


@pytest.mark.asyncio
async def test_a_mixed_route_wave_turns_one_route_at_a_time(tmp_path, *, cursor_delivery):
    """One selected turn answers originals of one reply route; the rest stay

    pending for their own turns, oldest first. Found when the batch answer
    became one plain text: a wave mixing a direct request and a channel
    request must never share that answer across audiences.
    """
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    for name in ("sender", "receiver"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model='openai-codex/gpt-6-sol'))
    root_id = comms.messaging.initialize_private_initial_protocol()
    lookup = RecordedThreadIncarnation("fixture", comms.registry.require("receiver").created_at).lookup
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(lookup, "receiver", "receiver", committed=True)

        def send(target, body):
            message = publish_initial(comms, "sender", target, body)
            accept_delivery_cohort(comms.bus, root_id, message.seq, store)
            return message

        direct = send("receiver", "A private question")          # reply route: sender
        channel = send("#team", "A team question")               # reply route: #team
        pending_wires = (direct.seq, channel.seq)
        pending = pending_sealed_assignments(store, lookup, "receiver")
        assert tuple(row.wire_seq for row in pending) == pending_wires
        async with SelectedParticipant.select(
            comms, store, root_id, "receiver", 0, cursor_delivery=cursor_delivery
        ) as selected:
            assert selected.batch.target == "sender"
            assert tuple(row.wire_seq for row in selected.batch.assignments) == (direct.seq,)
        # The channel original was never claimed by this turn: selection alone
        # settles nothing, so it stays pending for its own turn.
        assert tuple(row.wire_seq for row in
                     pending_sealed_assignments(store, lookup, "receiver")) == pending_wires

