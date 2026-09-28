"""Typed saved boundaries and durable acceptance precede process-local scheduling."""

import json
from dataclasses import dataclass, replace
from unittest.mock import patch

import pytest

from agent_comms import field_codec
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import StartedInput, UnknownInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.locked_store import LockedStore


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


@pytest.mark.parametrize(
    "damage", [{}, {"rows": {}}, {"version": True, "rows": {}}, {"version": 1, "rows": []}]
)
def test_invalid_documents_are_not_replaced_by_updates(tmp_path, damage):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    store.path.write_text(json.dumps(damage))
    original = store.path.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        store.update(lambda document: replace(document, rows={}))
    assert store.path.read_bytes() == original


def test_lost_durable_record_ack_retains_evidence_without_new_acceptance(tmp_path, monkeypatch):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    original = LockedStore._write_unlocked
    lost = False

    def lose_ack(self, text):
        nonlocal lost
        original(self, text)
        if self.path == store.path and not lost:
            lost = True
            raise OSError("durable input acknowledgement lost")

    monkeypatch.setattr(LockedStore, "_write_unlocked", lose_ack)
    args = dict(seq=1, owner="worker", admission=1, target="worker", text="Exact input")
    with pytest.raises(OSError, match="acknowledgement lost"):
        store.record("bus:1", **args)
    reopened = InputDispositions(store.path)
    before = reopened.read().rows["bus:1"]
    assert before.unattempted
    assert not reopened.record("bus:1", **args)
    assert reopened.read().rows["bus:1"] == before
    assert before.native_id is None and not before.goal_reviews
