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

@pytest.mark.asyncio
async def test_terminal_triage_failure_records_context_and_fails_its_claims(tmp_path, *, cursor_delivery):
    """A failed native triage leaves no input without a recorded context.

    Compaction requires a recorded context for every input sent into a session;
    an unrecorded failed input would block that session's compaction forever.
    """
    from agent_comms.assignment_states import FailedAssignment
    from agent_comms.coordination_errors import StaleFence
    from agent_comms.native_pi import NativeContextProof
    from agent_comms.native_session_reopen import NativeSessionIdentity

    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    for name in ("sender", "receiver"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model='openai-codex/gpt-6-sol'))
    root_id = comms.messaging.initialize_private_initial_protocol()
    lookup = RecordedThreadIncarnation("fixture", comms.registry.require("receiver").created_at).lookup
    session = NativeSessionIdentity("native-session", str(tmp_path / "session.jsonl"))
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(lookup, "receiver", "receiver", committed=True)
        message = publish_initial(comms, "sender", "#team", "Question")
        accept_delivery_cohort(comms.bus, root_id, message.seq, store)
        async with SelectedParticipant.select(
            comms, store, root_id, "receiver", 0, cursor_delivery=cursor_delivery
        ) as selected:
            stage = TriageNativeSend(selected.batch.assignments)
            token_digest = "b" * 64
            input_id = stage.reserve(store, selected.identity, token_digest)
            with store.session.writing() as db:
                reserved = NativeRuntimeInput.one(db, input_id=input_id)
                reserved.sent_owner_claimed_generation.record(reserved, db, 1, session)
            context = NativeContextProof(
                input_id, session.session_id, "entry", 7, "c" * 64, 1_791_964_800_007, session.path
            )
            settled = stage.fail_decision(store, selected.identity, input_id, token_digest, context)
            assert all(type(row.lifecycle) is FailedAssignment for row in settled)
            with store.session.read():
                db = store.session._connection
                row = NativeRuntimeInput.one(db, input_id=input_id)
                assert row.reference.recorded and row.verdict is None
            # The failed input is settled once; it is never sent or settled again.
            with TestCase().assertRaises(StaleFence):
                stage.fail_decision(store, selected.identity, input_id, token_digest, context)
