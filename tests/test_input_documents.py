"""Typed saved boundaries and durable acceptance precede process-local scheduling."""

import json
from dataclasses import dataclass, replace
from unittest.mock import patch

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms import field_codec
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import StartedInput, UnknownInput
from agent_comms.input_disposition import AcpDeliveryCursors, InputDispositions
from agent_comms.locked_store import LockedStore
from agent_comms.threads import Thread


def test_codec_reuses_declared_schema_but_decodes_each_changed_value():
    @dataclass(frozen=True)
    class Reading:
        sequence: int

    with patch.object(field_codec, "get_type_hints", wraps=field_codec.get_type_hints) as resolve:
        rows = [FieldCodec.decode(Reading, {"sequence": value}) for value in range(100)]
        assert resolve.call_count == 1
        assert [row.sequence for row in rows] == list(range(100))
        with pytest.raises(ValueError):
            FieldCodec.decode(Reading, {"sequence": True})


def test_saved_discriminator_and_optional_notices_roundtrip_without_replay(tmp_path):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    saved = {
        "version": 1,
        "rows": {
            "acp:old": {
                "key": "acp:old",
                "sequence": None,
                "owner": "worker",
                "admission": 1,
                "target": "worker",
                "source_text": "Retain me",
                "turn_id": "turn",
                "native_id": "a" * 32,
                "sent_text": "Exact sent text",
                "status": "unknown",
                "notice_dismissed": True,
                "goal_reviews": {"goal": {"goal_revision": 7, "turn_id": "review"}},
            }
        },
    }
    store.path.write_text(json.dumps(saved))
    before = store.path.read_bytes()
    document = store.read()
    row = document.rows["acp:old"]
    assert isinstance(row, UnknownInput) and not row.unattempted
    assert row.reviewed_for_goal("goal")
    assert store.path.read_bytes() == before
    assert store._encode(document) == saved
    assert not store.bind(row.key, admission=1, turn_id="again", native_id="b" * 32, text="again")
    assert not store.started(row.key, turn_id="wrong", native_id="a" * 32, text="Exact sent text")
    assert store.started(row.key, turn_id="turn", native_id="a" * 32, text="Exact sent text")
    assert isinstance(store.read().rows[row.key], StartedInput)
    assert not store.started(row.key, turn_id="turn", native_id="a" * 32, text="Exact sent text")
    assert row.unresolved  # prior snapshot is immutable and unchanged


@pytest.mark.parametrize("store_type", [InputDispositions, AcpDeliveryCursors])
@pytest.mark.parametrize(
    "damage", [{}, {"rows": {}}, {"version": True, "rows": {}}, {"version": 1, "rows": []}]
)
def test_invalid_documents_are_not_replaced_by_updates(tmp_path, store_type, damage):
    store = store_type(tmp_path / store_type.filename)
    store.path.write_text(json.dumps(damage))
    original = store.path.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        store.update(lambda document: replace(document, rows={}))
    assert store.path.read_bytes() == original


async def test_lost_durable_record_ack_never_advances_or_schedules_then_never_replays(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    project = tmp_path / "worker"
    await agent.new_session(str(project))
    comms.threads.register(Thread("peer", frozenset(), str(project)))
    message = comms.messaging.send_message("peer", "worker", "Exactly one durable input")
    store = agent.inputs.dispositions
    cursor_before = agent.inputs.delivery_cursors.path.read_bytes()
    original = LockedStore._write_unlocked
    lost = False

    def lose_ack(self, text):
        nonlocal lost
        original(self, text)
        if self.path == store.path and not lost:
            lost = True
            raise OSError("durable input acknowledgement lost")

    monkeypatch.setattr(LockedStore, "_write_unlocked", lose_ack)
    try:
        with pytest.raises(OSError, match="acknowledgement lost"):
            await agent.inputs.drain_inbox("worker")
        assert agent.inputs.delivery_cursors.path.read_bytes() == cursor_before
        assert not agent.inputs.pending_turns.get("worker")
        assert not agent.inputs.backend_inboxes
        row = store.read().rows[f"bus:{message.seq}"]
        assert row.unattempted
        # Reopening supplies durable evidence only, not the lost acceptance permit.
        await agent.inputs.drain_inbox("worker")
        assert not agent.inputs.pending_turns.get("worker")
        assert not agent.inputs.backend_inboxes
        assert store.read().rows[row.key] == row
    finally:
        await agent.shutdown()
