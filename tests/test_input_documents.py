"""Typed saved boundaries and durable acceptance precede process-local scheduling."""

import json
import sqlite3
from contextlib import ExitStack, closing
from dataclasses import replace

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, GoalId, InputId, TurnId
from agent_comms.input_attempt import (
    BoundUnknownInput,
    GoalInputDecision,
    InputAttempt,
    MissingInput,
    NotSentInput,
    ReservedInput,
    StartedInput,
    UnresolvedInput,
)
from agent_comms.input_disposition import InputDispositionRow, InputDispositions, InputDocument
from agent_comms.lifecycle_capability import Unresolved
from agent_comms.reported_turn import ReportedTurn
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from stored_inputs import insert_inputs, stored_inputs

TURN = TurnId('9b473845f684444f80a26586ae298d2b')


def test_original_batch_publication_preserves_prior_rows_and_reserves_only_new_inputs(tmp_path):
    store = InputDispositions(tmp_path)
    args = dict(seq=None, owner="worker", claim_scope=1, target="worker", text="Prior input")
    assert store.record("prior", **args)
    assert store.record("unknown", **args)
    assert store.bind(
        "unknown",
        claim_scope=1,
        turn_id=TurnId('cba06b5736faf67e54b07b561eae9439'),
        native_id=InputId("a" * 32),
        text="Original sent input",
    )
    prior = stored_inputs(store.root)
    first = ReservedInput("first", None, "worker", 1, "worker", "First input")
    second = ReservedInput("second", None, "worker", 1, "worker", "Second input")
    with ExitStack() as ownership:
        document = store.record_originals(
            ReservedInput("prior", None, "worker", 1, "worker", "Must not replace"),
            ReservedInput("unknown", None, "worker", 1, "worker", "Must not replay"),
            first, replace(first, source_text="Duplicate must not replace"), second,
            ownership=ownership,
        )
        assert document.originals((first.key, second.key)) == (first, second)
        assert document.rows[first.key] is first and document.rows[second.key] is second
        assert document.rows["prior"] == prior.rows["prior"]
        assert document.rows["unknown"] == prior.rows["unknown"]
        assert store.read(("prior", first.key)).rows.keys() == {"prior", first.key}
        saved = stored_inputs(store.root)
        assert store.record_originals() == InputDocument()
        assert not store.record("first", **args)
        assert stored_inputs(store.root) == saved
    retired = stored_inputs(store.root)
    assert isinstance(retired.rows[first.key], NotSentInput)
    assert isinstance(retired.rows[second.key], NotSentInput)
    assert retired.rows["prior"] == prior.rows["prior"]
    assert retired.rows["unknown"] == prior.rows["unknown"]


def test_failure_after_original_publication_retires_the_batch_and_schedule(tmp_path):
    store = InputDispositions(tmp_path)
    rows = tuple(ReservedInput(f"acp:original:{seq}", None, "worker", 1, "worker", "Original")
                 for seq in (1, 2))
    with pytest.raises(OSError), ExitStack() as ownership:
        store.record_originals(*rows, ownership=ownership)
        raise OSError("later failure")
    assert all(isinstance(row, NotSentInput) for row in stored_inputs(store.root).originals(
        tuple(row.key for row in rows)
    ))
    with pytest.raises(OSError), ExitStack() as ownership:
        store.reserve_turn(
            "worker",
            TurnId('6aef76c6cb359f6ffaf9c6e9881c902b'),
            1,
            "Original schedule",
            ownership=ownership,
        )
        raise OSError("later failure")
    scheduled = stored_inputs(store.root)
    assert all(isinstance(row, NotSentInput) for row in scheduled.rows.values())
    with ExitStack() as ownership, pytest.raises(RelationViolationError):
        store.reserve_turn(
            "worker",
            TurnId('6aef76c6cb359f6ffaf9c6e9881c902b'),
            1,
            "Replacement",
            ownership=ownership,
        )
    assert stored_inputs(store.root) == scheduled


def _insert_raw(store: InputDispositions, key: str, attempt: dict) -> None:
    insert_inputs(store.root, {key: attempt})


def test_saved_discriminator_and_optional_notices_roundtrip_without_replay(tmp_path):
    goal_id = GoalId.mint()
    store = InputDispositions(tmp_path)
    from agent_comms.text_digest import TextDigest

    saved = FieldCodec.encode(
        BoundUnknownInput(
            key="acp:old",
            sequence=None,
            owner="worker",
            claim_generation=1,
            target="worker",
            source_text="Retain me",
            turn_id=TURN,
            native_id=InputId("a" * 32),
            sent_digest=TextDigest.of("Exact sent text"),
            notice_dismissed=True,
            goal_reviews={
                goal_id: GoalInputDecision(
                    goal_revision=7,
                    turn_id=ReportedTurn(TurnId('c97ace4c8fef2cee8fa0f3c9f52aab18')),
                )
            },
        )
    )
    _insert_raw(store, "acp:old", saved)
    row = store.lookup("acp:old")
    assert isinstance(row, BoundUnknownInput) and not row.accepts_reservation
    assert row.reviewed_for_goal(goal_id)
    assert FieldCodec.encode(row) == saved
    assert store.in_turn(TURN).rows == {"acp:old": row}
    assert store.unresolved(frozenset({"worker"})).rows == {"acp:old": row}
    assert not store.bind(
        row.key,
        claim_scope=1,
        turn_id=TurnId('b4c9e14061c2fd453b36700e3b0da008'),
        native_id=InputId("b" * 32),
        text="again",
    )
    assert not store.started(
        row.key,
        turn_id=TurnId('8810ad581e59f2bc3928b261707a7130'),
        native_id=InputId("a" * 32),
        text="Exact sent text",
    )
    assert store.started(
        row.key,
        turn_id=TURN,
        native_id=InputId("a" * 32),
        text="Exact sent text",
    )
    assert isinstance(stored_inputs(store.root).rows[row.key], StartedInput)
    assert not store.started(
        row.key,
        turn_id=TURN,
        native_id=InputId("a" * 32),
        text="Exact sent text",
    )
    row.require(Unresolved, ValueError("review required"))  # prior snapshot is immutable and unchanged


def test_original_native_user_receipts_include_group_and_corrected_final_only(tmp_path):
    import os

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.registration import Registration

    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread('worker', frozenset(), str(tmp_path), created_at=123.0, process_identity=ProcessIdentity.capture(os.getpid())))
    registry.lease_local_turn("worker", TURN)
    lease = registry.require("worker").turn_lease
    claim_scope = lease.claimed_generation
    store = InputDispositions(tmp_path)
    for key, sequence, text in (("bus:1", 1, "First original"), ("bus:2", 2, "Second original")):
        assert store.record(key, seq=sequence, owner="worker", claim_scope=claim_scope,
                            target="worker", text=text)
        assert store.bind(
            key,
            claim_scope=claim_scope,
            turn_id=TURN,
            native_id=InputId("a" * 32),
            text="Actual grouped native user",
        )
        assert store.started(
            key,
            turn_id=TURN,
            native_id=InputId("a" * 32),
            text="Actual grouped native user",
        )
    registry.rename("worker", "renamed")
    assert store.record("acp:correction", seq=None, owner="renamed", claim_scope=claim_scope,
                        target="renamed", text="Distinct correction")
    assert store.bind(
        "acp:correction",
        claim_scope=claim_scope,
        turn_id=TURN,
        native_id=InputId("b" * 32),
        text="Actual corrected native user",
    )
    assert store.started(
        "acp:correction",
        turn_id=TURN,
        native_id=InputId("b" * 32),
        text="Actual corrected native user",
    )
    document = store.in_turn(TURN)
    assert document == stored_inputs(store.root)
    snapshot = registry.snapshot()
    group = document.started_for_native(
        lease, "a" * 32, "Actual grouped native user", snapshot=snapshot
    )
    assert {row.key for row in group} == {"bus:1", "bus:2"}
    assert all(row is document.rows[row.key] for row in group)
    assert document.started_for_native(
        lease.renamed("renamed"), "a" * 32, "Actual grouped native user", snapshot=snapshot
    ) == group
    assert document.started_for_native(
        lease, "b" * 32, "Actual corrected native user", snapshot=snapshot
    ) == (document.rows["acp:correction"],)
    assert not document.started_for_native(
        replace(lease, turn_id=TurnId('656771905e1ef731f65cd0a0d9fb0612')),
        "a" * 32,
        "Actual grouped native user",
        snapshot=snapshot,
    )
    assert not document.started_for_native(
        replace(lease, claimed_generation=claim_scope + 1), "a" * 32,
        "Actual grouped native user", snapshot=snapshot
    )
    assert not document.started_for_native(
        lease, "a" * 32, "Foreign input body", snapshot=snapshot
    )
    assert not document.started_for_native(
        lease, "c" * 32, "Actual grouped native user", snapshot=snapshot
    )
    assert not document.started_for_native(
        replace(lease, identity=replace(
            lease.identity, incarnation=RecordedThreadIncarnation("foreign", 124.0)
        )), "a" * 32, "Actual grouped native user", snapshot=snapshot
    )
    assert not document.started_for_native(
        replace(lease, identity=replace(
            lease.identity, incarnation=RecordedThreadIncarnation("worker", 124.0)
        )), "a" * 32, "Actual grouped native user", snapshot=snapshot
    )
    registry.release_turn(lease)
    registry.unregister("renamed")
    registry.remove("renamed")
    registry.register(Thread('renamed', frozenset(), str(tmp_path), created_at=321.0, process_identity=ProcessIdentity.capture(os.getpid())))
    assert not document.started_for_native(
        lease, "b" * 32, "Actual corrected native user", snapshot=registry.snapshot()
    )


def test_distinct_lifecycle_and_missing_state_never_supply_sent_evidence(tmp_path):
    from dataclasses import fields


    store = InputDispositions(tmp_path)
    missing = stored_inputs(store.root).lookup("acp:missing")
    assert isinstance(missing, MissingInput) and fields(missing) == ()
    assert not missing.exists and not missing.accepts_reservation and not missing.has_started
    assert not store.bind(
        "acp:missing",
        claim_scope=1,
        turn_id=TurnId('e3b98a4da31a127d4bde6e43033f66ba'),
        native_id=InputId("a" * 32),
        text="text",
    )
    assert not stored_inputs(store.root).all_started(("acp:missing",))
    assert store.record(
        "acp:input", seq=None, owner="owner", claim_scope=4, target="owner", text="keep"
    )
    reserved = stored_inputs(store.root).lookup("acp:input")
    assert isinstance(reserved, ReservedInput) and reserved.exists and reserved.accepts_reservation
    assert reserved.matches_owner(RecordedThreadIncarnation("owner", 10.0))
    # Existing input rows attest name/claim_scope only, never a historical birth.
    assert reserved.matches_owner(RecordedThreadIncarnation("owner", 20.0))
    assert not reserved.matches_owner(RecordedThreadIncarnation("other", 10.0))
    assert reserved.matches_claim(4) and not reserved.matches_claim(5)
    for name in ("native_id", "turn_id", "sent_digest"):
        assert name not in {item.name for item in fields(reserved)}
        assert not hasattr(reserved, name)
    unsent = store.settle_unbound(("acp:input",)).lookup("acp:input")
    unsent.require(Unresolved, ValueError("review required"))
    assert isinstance(unsent, NotSentInput)
    assert not unsent.accepts_reservation
    assert not store.bind(
        "acp:input",
        claim_scope=4,
        turn_id=TurnId('11507a0e2f5e69d5dfa40a62a1bd7b6e'),
        native_id=InputId("b" * 32),
        text="keep",
    )
    assert UnresolvedInput.of(unsent).status == "not_sent"
    assert "unknown" not in InputAttempt.names()
    with pytest.raises(ValueError):
        InputDocument(rows={"missing": missing})


def test_batch_retirement_returns_published_cut_without_changing_native_evidence(tmp_path):
    store = InputDispositions(tmp_path)
    keys = ("acp:reserved", "acp:bound", "acp:started", "acp:unsent", "acp:unrelated")
    for key in keys:
        assert store.record(key, seq=None, owner="owner", claim_scope=1,
                            target="owner", text=key)
    for key in ("acp:bound", "acp:started"):
        assert store.bind(key, claim_scope=1, turn_id=TurnId('ab87f599dc0a50a56b7c65de71c39426'),
                          native_id=InputId("a" * 32), text=key)
    assert store.started(
        "acp:started",
        turn_id=TurnId('ab87f599dc0a50a56b7c65de71c39426'),
        native_id=InputId("a" * 32),
        text="acp:started",
    )
    store.settle_unbound(("acp:unsent",))
    original = stored_inputs(store.root)
    selected = (*keys[:-1], "acp:missing")
    published = store.settle_unbound(selected)
    assert stored_inputs(store.root).rows == {**original.rows, **published.rows}
    assert isinstance(published.lookup("acp:reserved"), NotSentInput)
    assert published.lookup("acp:reserved").context_provenance() == (
        original.lookup("acp:reserved").context_provenance()
    )
    assert published.lookup("acp:reserved").source_text == "acp:reserved"
    assert {key: published.lookup(key) for key in keys[1:4]} == {
        key: original.lookup(key) for key in keys[1:4]
    }
    assert isinstance(published.lookup("acp:missing"), MissingInput)
    assert "acp:missing" not in published.rows
    assert store.settle_unbound(selected) == published
    assert store.settle_unbound(()) == InputDocument()
    assert store.read(selected) == published


@pytest.mark.parametrize(
    "sent",
    [
        {"turn_id": "9b473845f684444f80a26586ae298d2b"},
        {"native_id": "a" * 32},
        {"turn_id": "9b473845f684444f80a26586ae298d2b", "native_id": "a" * 32},
    ],
)
def test_incomplete_sent_evidence_is_rejected_without_overwriting_history(tmp_path, sent):
    store = InputDispositions(tmp_path)
    row = FieldCodec.encode(ReservedInput("acp:x", None, "owner", 1, "owner", "text"))
    row.update(kind="bound_unknown", **sent)
    _insert_raw(store, "acp:x", row)
    with pytest.raises(ValueError):
        store.settle_unbound(("acp:x",))
    with closing(sqlite3.connect(store.path)) as connection:
        assert json.loads(connection.execute('SELECT attempt FROM "input_disposition"').fetchone()[0]) == row


@pytest.mark.parametrize("native_id", ["invalid", "b" * 31])
def test_invalid_bind_keeps_durable_reservation_intact(tmp_path, native_id):
    store = InputDispositions(tmp_path)
    store.record("acp:x", seq=None, owner="owner", claim_scope=1, target="owner", text="retained")
    before = stored_inputs(store.root)
    with pytest.raises(ValueError):
        store.bind(
            "acp:x",
            claim_scope=1,
            turn_id=TURN,
            native_id=native_id,
            text="sent",
        )
    assert stored_inputs(store.root) == before


def test_reserved_input_is_queued_only_for_its_own_owner_claim_and_text():
    from agent_comms.text_digest import TextDigest

    reserved = ReservedInput("acp:x", None, "owner", 1, "owner", "original")
    bound = reserved.bind(
        claim_scope=1,
        turn_id=TURN,
        native_id=InputId("a" * 32),
        text="wrapped input",
    )
    bound = bound.require_moved("Reserved input stayed reserved").state
    owner = RecordedThreadIncarnation("owner", 1.0)
    assert reserved.digest == TextDigest.of("original")
    assert reserved.queued_for(owner, 1, "original")
    assert not reserved.queued_for(owner, 1, "different")
    assert not bound.queued_for(owner, 1, "original")


@pytest.mark.parametrize(
    ("key", "sequence", "target"),
    [("acp:x", None, "owner"), ("acp:x", None, "#channel"), ("bus:1", 1, "#channel")],
    ids=["owner", "channel", "bus"],
)
def test_started_delivery_and_original_ingress_use_distinct_owned_relations(
    tmp_path, key, sequence, target
):
    store = InputDispositions(tmp_path)
    assert store.record(
        key, seq=sequence, owner="owner", claim_scope=1, target=target, text="original"
    )
    assert store.bind(
        key,
        claim_scope=1,
        turn_id=TURN,
        native_id=InputId("a" * 32),
        text="wrapped input",
    )
    assert store.started(
        key,
        turn_id=TURN,
        native_id=InputId("a" * 32),
        text="wrapped input",
    )
    assert stored_inputs(store.root).lookup(key).has_started
    original = stored_inputs(store.root)
    # Routing cannot be rewritten by recording another target at the original
    # key. That ownership belongs to first durable acceptance, not start proof.
    assert not store.record(
        key, seq=sequence, owner="owner", claim_scope=1, target="#foreign", text="original"
    )
    assert stored_inputs(store.root) == original
    assert stored_inputs(store.root).lookup(key).target == target
    assert stored_inputs(store.root).lookup(key).has_started


def test_reads_select_only_the_rows_their_index_names(tmp_path):
    store = InputDispositions(tmp_path)
    for key, owner in (("acp:a", "owner"), ("acp:b", "owner"), ("acp:c", "other")):
        assert store.record(key, seq=None, owner=owner, claim_scope=1, target=owner, text=key)
    assert store.bind(
        "acp:a",
        claim_scope=1,
        turn_id=TURN,
        native_id=InputId("a" * 32),
        text="sent",
    )
    assert store.started(
        "acp:a", turn_id=TURN, native_id=InputId("a" * 32), text="sent"
    )
    assert store.in_turn(TURN).rows.keys() == {"acp:a"}
    assert store.unresolved(frozenset({"owner"})).rows.keys() == {"acp:b"}
    assert store.read(("acp:c", "acp:missing")).rows.keys() == {"acp:c"}
    with closing(sqlite3.connect(store.path)) as connection:
        plans = [connection.execute("EXPLAIN QUERY PLAN SELECT * FROM input_disposition WHERE " + where,
                                    parameters).fetchall()
                 for where, parameters in (('"turn_id" = ?', ("turn",)),
                                           ('unresolved = 1 AND "owner" IN (?)', ("owner",)))]
    assert [plan[-1][-1].split(" USING ")[1] for plan in plans] == [
        'INDEX input_disposition_turn_id_where_turn_id_is_not_null_idx (turn_id=?)',
        'INDEX input_disposition_owner_unresolved_idx (owner=? AND unresolved=?)']


def test_stored_row_declaration_owns_the_schema(tmp_path):
    store = InputDispositions(tmp_path)
    store.record_originals()
    with closing(sqlite3.connect(store.path)) as connection:
        connection.execute('DROP INDEX "input_disposition_turn_id_where_turn_id_is_not_null_idx"')
        connection.commit()
    with pytest.raises(RelationViolationError):
        store.read(())
    assert InputDispositionRow.declared_name == "input_disposition"
