"""Typed saved boundaries and durable acceptance precede process-local scheduling."""

import json
from dataclasses import dataclass, replace
from unittest.mock import patch

import pytest

from agent_comms import field_codec
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import (
    BoundUnknownInput,
    InputAttempt,
    MissingInput,
    NotSentInput,
    ReservedInput,
    StartedInput,
)
from agent_comms.input_disposition import InputDispositions, InputDocument
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
                "kind": "bound_unknown",
                "notice_dismissed": True,
                "goal_reviews": {"goal": {"goal_revision": 7, "turn_id": "review"}},
            }
        },
    }
    store.path.write_text(json.dumps(saved))
    before = store.path.read_bytes()
    document = store.read()
    row = document.rows["acp:old"]
    assert isinstance(row, BoundUnknownInput) and not row.accepts_reservation
    assert row.reviewed_for_goal("goal")
    assert store.path.read_bytes() == before
    assert store._encode(document) == saved
    assert not store.bind(row.key, admission=1, turn_id="again", native_id="b" * 32, text="again")
    assert not store.started(row.key, turn_id="wrong", native_id="a" * 32, text="Exact sent text")
    assert store.started(row.key, turn_id="turn", native_id="a" * 32, text="Exact sent text")
    assert isinstance(store.read().rows[row.key], StartedInput)
    assert not store.started(row.key, turn_id="turn", native_id="a" * 32, text="Exact sent text")
    assert row.unresolved  # prior snapshot is immutable and unchanged


def test_original_native_user_receipts_include_group_and_corrected_final_only(tmp_path):
    from agent_comms.thread_identity import ThreadIncarnation, TurnIdentity
    from agent_comms.turn_lease import TurnLeaseFence

    lease = TurnLeaseFence(TurnIdentity(ThreadIncarnation('worker', 123.0), 5), 'turn', 7)
    store = InputDispositions(tmp_path / InputDispositions.filename)
    for key, sequence, text in (('bus:1', 1, 'First original'), ('bus:2', 2, 'Second original')):
        assert store.record(key, seq=sequence, owner='worker', admission=7,
                            target='worker', text=text)
        assert store.bind(key, admission=7, turn_id='turn', native_id='a' * 32,
                          text='Actual grouped native user')
        assert store.started(key, turn_id='turn', native_id='a' * 32,
                             text='Actual grouped native user')
    assert store.record('acp:correction', seq=None, owner='worker', admission=7,
                        target='worker', text='Distinct correction')
    assert store.bind('acp:correction', admission=7, turn_id='turn', native_id='b' * 32,
                      text='Actual corrected native user')
    assert store.started('acp:correction', turn_id='turn', native_id='b' * 32,
                         text='Actual corrected native user')
    document = store.read()
    group = document.started_for_native(lease, 'a' * 32, 'Actual grouped native user')
    assert {row.key for row in group} == {'bus:1', 'bus:2'}
    assert all(row is document.rows[row.key] for row in group)
    assert document.started_for_native(lease, 'b' * 32, 'Actual corrected native user') == (
        document.rows['acp:correction'],)
    assert not document.started_for_native(replace(lease, turn_id='foreign'), 'a' * 32,
                                           'Actual grouped native user')
    assert not document.started_for_native(replace(lease, admission_generation=5), 'a' * 32,
                                           'Actual grouped native user')
    assert not document.started_for_native(lease, 'a' * 32, 'Foreign input body')
    assert not document.started_for_native(lease, 'c' * 32, 'Actual grouped native user')
    assert not document.started_for_native(replace(lease, identity=replace(
        lease.identity, incarnation=ThreadIncarnation('foreign', 123.0))), 'a' * 32,
        'Actual grouped native user')


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
    assert before.accepts_reservation
    assert not reopened.record("bus:1", **args)
    assert reopened.read().rows["bus:1"] == before
    assert not hasattr(before, "native_id") and not before.goal_reviews


def test_distinct_lifecycle_and_missing_state_never_supply_sent_evidence(tmp_path):
    from dataclasses import fields

    from agent_comms.thread_identity import ThreadIncarnation

    store = InputDispositions(tmp_path / InputDispositions.filename)
    missing = store.read().lookup("acp:missing")
    assert isinstance(missing, MissingInput) and fields(missing) == ()
    assert not missing.exists and not missing.accepts_reservation and not missing.has_started
    assert not store.bind("acp:missing", admission=1, turn_id="t", native_id="a" * 32, text="text")
    assert not store.read().all_started(("acp:missing",))
    assert store.record(
        "acp:input", seq=None, owner="owner", admission=4, target="owner", text="keep"
    )
    reserved = store.read().lookup("acp:input")
    assert isinstance(reserved, ReservedInput) and reserved.exists and reserved.accepts_reservation
    assert reserved.matches_owner(ThreadIncarnation("owner", 10.0))
    # Existing input rows attest name/admission only, never a historical birth.
    assert reserved.matches_owner(ThreadIncarnation("owner", 20.0))
    assert not reserved.matches_owner(ThreadIncarnation("other", 10.0))
    assert reserved.matches_admission(4) and not reserved.matches_admission(5)
    for name in ("native_id", "turn_id", "sent_text"):
        assert name not in {item.name for item in fields(reserved)}
        assert not hasattr(reserved, name)
    assert store.settle_unbound(("acp:input",))
    unsent = store.read().lookup("acp:input")
    assert isinstance(unsent, NotSentInput) and unsent.unresolved
    assert not unsent.accepts_reservation
    assert not store.bind("acp:input", admission=4, turn_id="new", native_id="b" * 32, text="keep")
    assert unsent.public()["status"] == "not_sent"
    assert "unknown" not in InputAttempt.names()
    with pytest.raises(ValueError, match="document key"):
        InputDocument(rows={"missing": missing})


@pytest.mark.parametrize(
    "sent",
    [
        {"turn_id": "turn"},
        {"native_id": "a" * 32},
        {"turn_id": "turn", "native_id": "a" * 32},
        {"turn_id": "", "native_id": "a" * 32, "sent_text": "exact"},
    ],
)
def test_incomplete_sent_evidence_is_rejected_without_overwriting_history(tmp_path, sent):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    row = FieldCodec.encode(ReservedInput("acp:x", None, "owner", 1, "owner", "text"))
    row.update(kind="bound_unknown", **sent)
    saved = json.dumps(dict(version=1, rows={"acp:x": row}))
    store.path.write_text(saved)
    with pytest.raises(ValueError):
        store.settle_unbound(("acp:x",))
    assert store.path.read_text() == saved


@pytest.mark.parametrize("native_id", ["invalid", "b" * 31])
def test_invalid_bind_keeps_durable_reservation_intact(tmp_path, native_id):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    store.record("acp:x", seq=None, owner="owner", admission=1, target="owner", text="retained")
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="native input"):
        store.bind("acp:x", admission=1, turn_id="turn", native_id=native_id, text="sent")
    assert store.path.read_bytes() == before


def test_current_schema_rejects_nullable_prior_format_without_rewriting(tmp_path):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    reserved = ReservedInput("acp:x", None, "owner", 1, "owner", "preserve at cutover")
    prior = {
        **FieldCodec.encode(reserved),
        "status": "unknown",
        "turn_id": None,
        "native_id": None,
        "sent_text": None,
    }
    del prior["kind"]
    store.path.write_text(json.dumps(dict(version=1, rows={reserved.key: prior})))
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.read()
    assert store.path.read_bytes() == before


def test_current_reservation_wire_has_only_declared_fields(tmp_path):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    store.record("acp:x", seq=None, owner="owner", admission=1, target="owner", text="retained")
    record = json.loads(store.path.read_text())["rows"]["acp:x"]
    assert record["kind"] == "reserved"
    assert not {"status", "native_id", "turn_id", "sent_text"}.intersection(record)
    assert store.settle_unbound(("acp:x",))
    record = json.loads(store.path.read_text())["rows"]["acp:x"]
    assert record["kind"] == "not_sent"
    assert not {"native_id", "turn_id", "sent_text"}.intersection(record)


@pytest.mark.parametrize(
    "mismatch", [None, "owner", "admission", "turn", "sent", "original", "target", "bus"]
)
def test_only_exact_started_input_proves_original(mismatch):
    from agent_comms.text_digest import TextDigest

    from agent_comms.thread_identity import ThreadIncarnation, TurnId

    reserved = ReservedInput("acp:x", None, "owner", 1, "owner", "original")
    bound = reserved.bind(admission=1, turn_id="turn", native_id="a" * 32, text="wrapped input")
    started = bound.started(turn_id="turn", native_id="a" * 32, text="wrapped input")
    proof = dict(
        owner=ThreadIncarnation("owner", 1.0),
        admission=1,
        turn=TurnId("turn"),
        sent_digest=TextDigest.of("wrapped input"),
        original_digest=TextDigest.of("original"),
    )
    for state in (MissingInput(), reserved, bound, reserved.finish_unbound()):
        assert not state.proves_started(**proof)
    assert reserved.digest == TextDigest.of("original")
    assert bound.sent_digest == TextDigest.of("wrapped input")
    assert reserved.queued_for(proof["owner"], 1, "original")
    assert not reserved.queued_for(proof["owner"], 1, "different")
    assert not bound.queued_for(proof["owner"], 1, "original")
    if mismatch == "owner":
        proof["owner"] = ThreadIncarnation("other", 1.0)
    elif mismatch == "admission":
        proof["admission"] = 2
    elif mismatch == "turn":
        proof["turn"] = TurnId("different")
    elif mismatch == "sent":
        proof["sent_digest"] = TextDigest.of("different wrapper")
    elif mismatch == "original":
        proof["original_digest"] = TextDigest.of("different original")
    elif mismatch == "target":
        started = replace(started, target="#channel")
    elif mismatch == "bus":
        started = replace(started, key="bus:1", sequence=1)
    assert started.proves_started(**proof) is (mismatch is None)
