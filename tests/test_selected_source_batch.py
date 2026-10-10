"""Canonical original-bus batch capture and one-use native reservation."""

import json
import os
import pytest

from unittest import TestCase

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, pending_sealed_assignments
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.native_prompt_binding import bind_expected_prompt, read_expected_prompt_binding
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.private_send_stage import TriageNativeSend
from agent_comms.selected_participant import SelectedParticipant
from agent_comms.selected_turn import SelectedPrompt
from agent_comms.threads import Thread


@pytest.mark.asyncio
async def test_original_pending_wave_has_one_fenced_input_and_late_arrivals_stay_pending(tmp_path):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    for name in ("sender", "receiver"):
        comms.registry.declare(Thread(
            name, frozenset({"team"}), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            model="openai-codex/gpt-6-sol",
        ))
    root_id = comms.messaging.initialize_private_initial_protocol()
    lookup = stable_thread_lookup(comms.registry.require("receiver").created_at)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(lookup, "receiver", "receiver", committed=True)

        def send(body):
            message = comms.messaging.send_initial_cohort("sender", "#team", body)
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
        async with SelectedParticipant.select(comms, store, root_id, "receiver", 0,
                                              on_compaction=publish_compaction) as selected:
            from agent_comms.agent_events import CompactionStart, CompactionSummaryProgress, CompactionEnd
            observations = (CompactionStart(), CompactionSummaryProgress(text="Partial original summary"),
                            CompactionEnd(summary="Committed original summary"))
            for observation in observations:
                await selected.dispatch(observation)
            assert len(published) == 3
            assert all(actual is original for actual, original in zip(published, observations, strict=True))
            assert selected.batch.assignments == snapshot
            prompt = SelectedPrompt(selected).triage().text
            from agent_comms.wake_policy import WakePolicy

            # The original instruction is shared by the whole captured batch,
            # rather than multiplying mandatory context for every source row.
            assert prompt.count(WakePolicy.relevance_instruction().content) == 1
            # Mandatory original content belongs to native selected-model admission,
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
            binding = read_expected_prompt_binding(store, input_id)
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


@pytest.mark.asyncio
async def test_terminal_triage_failure_records_context_and_fails_its_claims(tmp_path):
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
    comms = Comms(root, private_initial_writes=True)
    for name in ("sender", "receiver"):
        comms.registry.declare(Thread(
            name, frozenset({"team"}), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            model="openai-codex/gpt-6-sol",
        ))
    root_id = comms.messaging.initialize_private_initial_protocol()
    lookup = stable_thread_lookup(comms.registry.require("receiver").created_at)
    session = NativeSessionIdentity("native-session", str(tmp_path / "session.jsonl"))
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(lookup, "receiver", "receiver", committed=True)
        message = comms.messaging.send_initial_cohort("sender", "#team", "Question")
        accept_delivery_cohort(comms.bus, root_id, message.seq, store)
        async with SelectedParticipant.select(comms, store, root_id, "receiver", 0) as selected:
            stage = TriageNativeSend(selected.batch.assignments)
            token_digest = "b" * 64
            input_id = stage.reserve(store, selected.identity, token_digest)
            with store.session.transaction() as db:
                reserved = NativeRuntimeInput.one(db, input_id=input_id)
                reserved.sent_owner_admission_generation.record(reserved, db, 1, session)
            context = NativeContextProof(
                input_id, session.session_id, "entry", 7, "c" * 64, session.path
            )
            settled = stage.fail_terminal(store, selected.identity, input_id, token_digest, context)
            assert all(type(row.lifecycle) is FailedAssignment for row in settled)
            with store.session.read():
                db = store.session._connection
                row = NativeRuntimeInput.one(db, input_id=input_id)
                assert row.reference.recorded and row.verdict is None
                assert NativeRuntimeInput.recorded_contexts(db, session) == {input_id: context}
            # The failed input is settled once; it is never sent or settled again.
            with TestCase().assertRaises(StaleFence):
                stage.fail_terminal(store, selected.identity, input_id, token_digest, context)
