"""Ordinary Comms sends on explicitly marked private roots execute selected N/K.

Fake model responses exercise pipeline state only; no native/provider acceptance
is inferred. Public roots remain legacy and no cutover is performed by send.
"""

from __future__ import annotations

import json
import os

import pytest

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination import ClaimDisposition
from agent_comms.coordination_store import MutationStore
from agent_comms.declarations import Message, MessageType, RelationViolationError, Thread
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.operations import Comms
from agent_comms.tools import invoke_tool
from test_coordinated_runtime import _fake_model
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.mark.parametrize(
    ("target", "body", "decision", "expected_calls", "expected_k", "disposition"),
    [
        ("beta", "Compute 17+25", "FULL", 1, 1, ClaimDisposition.COMPLETED),
        ("#team", "@beta Compute 17+25", "FULL", 1, 1, ClaimDisposition.COMPLETED),
        ("#team", "Compute 17+25", "IGNORE", 1, 2, ClaimDisposition.IGNORED),
        ("#team", "Compute 17+25", "FULL", 2, 2, ClaimDisposition.COMPLETED),
        ("#team", "@alpha Compute 17+25", "FULL", 0, 1, None),
    ],
)
async def test_normal_send_to_existing_foreground_executes_exact_nk(
    tmp_path, monkeypatch, target, body, decision, expected_calls, expected_k, disposition
):
    root = tmp_path / "wire"
    comms = Comms(root)
    comms.register(Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()))
    alpha = Thread("alpha", frozenset({"team"}), str(tmp_path), pid=os.getpid())
    comms.register(alpha)
    root_id = comms.initialize_private_initial_protocol()
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
        # The ordinary public API, not send_initial_cohort or a candidate bridge.
        receipt = invoke_tool(comms, "comms_send", {"from": "sender", "to": target, "body": body})
        original.append(comms.bus.message_by_id(receipt["id"]))

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
    initial = comms.bus.read_initial_cohort(root_id, message.seq)
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
            db.execute("SELECT COUNT(*) FROM native_runtime_inputs").fetchone()[0] == expected_calls
        )
    # A no-wake observer cannot be turned into a selected run by an inbox ACK.
    comms.acknowledge_through("beta", message.seq)
    assert len(calls) == expected_calls
    # Private N/K metadata stays off the normal public message projection.
    assert PRIVATE_WIRE_FIELD not in message.to_wire()


def test_unmarked_ordinary_send_does_not_install_or_infer_cohort(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
    message = comms.send_message("sender", "beta", "legacy ordinary send")
    meta = json.loads((comms.root / "bus_meta.json").read_text())
    assert "writer_protocol_version" not in meta
    assert not (comms.root / "coordination.sqlite3").exists()
    assert comms.bus.message_by_id(message.message_id) == message


def test_legacy_writer_and_explicitly_disabled_private_writer_still_refuse(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
    root_id = comms.initialize_private_initial_protocol()
    with pytest.raises(RelationViolationError, match="Legacy append"):
        comms.bus.publish(
            Message(sender="sender", target="beta", body="old writer", type=MessageType.INFO)
        )
    disabled = Comms(comms.root, private_initial_writes=False)
    with pytest.raises(RelationViolationError, match="Private initial publication is disabled"):
        disabled.send_message("sender", "beta", "disabled writer")
    meta = json.loads((comms.root / "bus_meta.json").read_text())
    assert meta["wire_root_id"] == root_id and meta["last_seq"] == 0
    assert not (comms.root / "bus.jsonl").exists()
