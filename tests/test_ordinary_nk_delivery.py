"""Ordinary Comms sends on explicitly marked private roots execute selected N/K.

Model fixtures exercise pipeline state; actual native acceptance is checked
separately. Every ordinary send uses the canonical publication protocol.
"""

from __future__ import annotations

import json
import os

import pytest

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.assignment_states import CompletedAssignment, IgnoredAssignment
from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordination_store import MutationStore
from agent_comms.errors import RelationViolationError
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.messages import Message, MessageType
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool
from test_coordinated_runtime import _fake_model
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.mark.parametrize(
    ("target", "body", "decision", "expected_calls", "expected_k", "disposition"),
    [
        ("beta", "Compute 17+25", "FULL", 1, 1, CompletedAssignment),
        ("#team", "@beta Compute 17+25", "FULL", 1, 1, CompletedAssignment),
        ("#team", "Compute 17+25", "IGNORE", 1, 2, IgnoredAssignment),
        ("#team", "Compute 17+25", "FULL", 2, 2, CompletedAssignment),
        ("#team", "@alpha Compute 17+25", "FULL", 0, 1, None),
    ],
)
async def test_normal_send_to_existing_foreground_executes_exact_nk(
    tmp_path, monkeypatch, target, body, decision, expected_calls, expected_k, disposition
):
    root = tmp_path / "wire"
    comms = Comms(root)
    comms.threads.register(
        Thread(
            "sender",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    alpha = Thread(
        "alpha",
        frozenset({"team"}),
        str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
        model="openai-codex/gpt-6-sol",
    )
    comms.threads.register(alpha)
    root_id = comms.messaging.initialize_private_initial_protocol()
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        store.register_participant(
            stable_thread_lookup(alpha.created_at), "alpha", "alpha", committed=True
        )
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision=decision)
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    original = []
    recipient = []

    def ready(beta):
        recipient.append(beta)
        comms.threads.set_thread_model(beta.name, "openai-codex/gpt-6-sol")
        # The ordinary public API, not send_initial_cohort or a candidate bridge.
        receipt = invoke_tool(comms, "comms_send", {"from": "sender", "to": target, "body": body})
        original.append(comms.bus.log.message_by_id(receipt["id"]))

    result = await cohort_foreground.run_foreground_once(
        root,
        wire_root_id=root_id,
        name="beta",
        worktree=tmp_path,
        tags=frozenset({"team"}),
        native_package=tmp_path,
        wait_seconds=0,
        ready=ready,
    )
    assert len(calls) == expected_calls
    if disposition is None:
        assert isinstance(result, cohort_foreground.NoWakeReceipt)
    else:
        assert result.disposition is disposition
    message = original[0]
    initial = comms.bus.log.read_initial_cohort(root_id, message.seq)
    assert initial.message == message
    expected_n = 1 if target == "beta" else 2
    assert len(initial.audience.recipients) == expected_n
    lookup = stable_thread_lookup(recipient[0].created_at)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        db = store._connection
        assert (
            db.execute("SELECT COUNT(*) FROM cohort_delivery_receipts").fetchone()[0] == expected_n
        )
        assert db.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == expected_k
        rows = read_historical_native_inputs(
            store, wire_root_id=root_id, recipient_lookup=lookup, source_seq=message.seq
        )
        assert len(rows) == expected_calls
        assert all(row.expected_prompt_equality_established for row in rows)
        assert (
            db.execute(f"SELECT COUNT(*) FROM {NativeRuntimeInput.declared_name}").fetchone()[0]
            == expected_calls
        )
    # A no-wake observer cannot be turned into a selected run by an inbox ACK.
    comms.messaging.acknowledge_through("beta", message.seq)
    assert len(calls) == expected_calls
    # Private N/K metadata stays off the normal public message projection.
    assert PRIVATE_WIRE_FIELD not in message.to_wire()


def test_fresh_send_uses_canonical_publication_and_human_delivery_has_no_wake(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.threads.register(
            Thread(
                name,
                frozenset(),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
    message = comms.messaging.send_message("sender", "beta", "ordinary send")
    metadata = comms.bus.log.read_metadata_unlocked(required=True)
    assert metadata.private and metadata.claims
    initial = comms.bus.log.read_initial_cohort(metadata.root_id, message.seq)
    assert initial.message == message
    assert initial.audience.canonical_members == frozenset({"beta"})
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    reply = comms.messaging.send_message("sender", viewer, "human notification")
    human = comms.bus.log.read_initial_cohort(metadata.root_id, reply.seq)
    assert human.message.notice
    assert human.audience.recipients == ()
    assert human.decisions == ()
    assert comms.views.dm_display_page("sender", worktree=str(tmp_path)).messages[-1] == reply


def test_unmarked_existing_data_is_not_rewritten_or_appended_by_send(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.threads.register(
            Thread(
                name,
                frozenset(),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
    stored = Message(
        sender="sender", target="beta", body="preserved history", type=MessageType.INFO, seq=1
    )
    comms.bus.log.path.write_text(json.dumps(stored.to_wire()) + "\n")
    before = comms.bus.log.path.read_bytes()
    with pytest.raises(
        RelationViolationError, match="Bus protocol marker is missing or redirected"
    ):
        comms.messaging.send_message("sender", "beta", "must not append")
    assert comms.bus.log.path.read_bytes() == before
    assert not comms.bus.log.metadata_path.exists()


def test_explicitly_disabled_private_writer_refuses(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.threads.register(
            Thread(
                name,
                frozenset(),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    disabled = Comms(comms.root, private_initial_writes=False)
    with pytest.raises(RelationViolationError, match="Private initial publication is disabled"):
        disabled.messaging.send_message("sender", "beta", "disabled writer")
    meta = json.loads((comms.root / "bus_meta.json").read_text())
    assert meta["wire_root_id"] == root_id and meta["last_seq"] == 0
    assert not (comms.root / "bus.jsonl").exists()
