"""Cohort rows enforce their declared relations without a second claim discriminator."""

from __future__ import annotations

import os
import sqlite3
from contextlib import nullcontext
from pathlib import Path

import pytest
from bus_record_fixture import OWNER_LOOKUP, RECIPIENT_LOOKUP, SENDER_LOOKUP

from agent_comms.cohort_schema import CohortDeliveries, CohortTable
from agent_comms.field_codec import AssignmentId, FieldCodec, ThreadLookup
from agent_comms.coordination_tables.participants import Participants
from agent_comms.coordinator import Coordination
from agent_comms.coordination_database import CoordinationStore

A, B, C = SENDER_LOOKUP, RECIPIENT_LOOKUP, OWNER_LOOKUP
ROOT = "a" * 32
CLAIM_A = AssignmentId("c" * 32)
CLAIM_B = AssignmentId("d" * 32)
SEQ = 7


def _tables(store: Coordination) -> set[str]:
    return {
        str(row[0])
        for row in store.session._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _receipt(
    db: sqlite3.Connection, *, n: int, k: int, root: str = ROOT,
    message_id: str = "msg", policy_version: str = "p1", accepted_at_ms: int = 100,
) -> None:
    db.execute(
        "INSERT INTO claim_batch_receipts (wire_root_id,wire_seq,message_id,exact_target,"
        "envelope_digest,audience_digest,decisions_digest,member_count,claim_count,"
        "manifest_codec,resolver_version,policy_version,accepted_at_ms,sealed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (root, SEQ, message_id, "#team", "b" * 64, "c" * 64, "d" * 64,
         n, k, "v1", "r1", policy_version, accepted_at_ms),
    )


def _assignment(db: sqlite3.Connection) -> None:
    Participants(participant_lookup=A, committed=True).insert(db)
    Participants(participant_lookup=B, committed=True).insert(db)
    db.execute(
        "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,"
        "message_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
        "updated_at_ms,revision) VALUES (?,?,?,7,'msg',json_object('kind','triage_pending'),"
        "'collective','r1','p1',100,100,1)",
        (CLAIM_A, "Alice", A),
    )


def _second_assignment(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,"
        "message_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
        "updated_at_ms,revision) SELECT ?,?,?,wire_seq,message_id,lifecycle,audience,"
        "resolver_version,policy_version,accepted_at_ms,updated_at_ms,revision "
        "FROM wake_claims WHERE assignment_id=?",
        (CLAIM_B, "Bob", B, CLAIM_A),
    )


def _member(
    db: sqlite3.Connection, *, ordinal: int = 0,
    assignment_id: str = CLAIM_A, lookup: ThreadLookup = A, root: str = ROOT,
) -> None:
    db.execute(
        "INSERT INTO claim_batch_members VALUES (?,?,?,?,?)",
        (root, SEQ, ordinal, assignment_id, lookup),
    )


def _delivery(
    db: sqlite3.Connection, *, ordinal: int, lookup: ThreadLookup,
    name: str, assignment_id: str | None,
) -> None:
    db.execute(
        "INSERT INTO cohort_delivery_receipts VALUES (?,?,?,?,?,?)",
        (ROOT, SEQ, ordinal, lookup, name, assignment_id),
    )


def _seal(db: sqlite3.Connection) -> None:
    db.execute(
        "UPDATE claim_batch_receipts SET sealed=1 WHERE wire_root_id=? AND wire_seq=?",
        (ROOT, SEQ),
    )


def _selected_and_observer(db: sqlite3.Connection) -> None:
    _member(db)
    _delivery(db, ordinal=0, lookup=A, name="Alice", assignment_id=CLAIM_A)
    _delivery(db, ordinal=1, lookup=B, name="Bob", assignment_id=None)


@pytest.mark.parametrize("reverse_claims", (False, True))
def test_selected_claims_follow_ordered_n_subsequence_not_only_set_membership(
    private_comms, reverse_claims: bool,
) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with (
            pytest.raises(sqlite3.IntegrityError) if reverse_claims else nullcontext(),
            store.session.writing() as db,
        ):
            _assignment(db)
            Participants(participant_lookup=C, committed=True).insert(db)
            _second_assignment(db)
            _receipt(db, n=3, k=2)
            ordered = ((CLAIM_A, A), (CLAIM_B, B))
            if reverse_claims:
                ordered = ordered[::-1]
            for ordinal, (assignment_id, lookup) in enumerate(ordered):
                _member(db, ordinal=ordinal, assignment_id=assignment_id, lookup=lookup)
            _delivery(db, ordinal=0, lookup=A, name="Alice", assignment_id=CLAIM_A)
            _delivery(db, ordinal=1, lookup=C, name="Cara", assignment_id=None)
            _delivery(db, ordinal=2, lookup=B, name="Bob", assignment_id=CLAIM_B)
            _seal(db)
        rows = store.session._connection.execute("SELECT sealed FROM claim_batch_receipts").fetchall()
        assert [row[0] for row in rows] == ([] if reverse_claims else [1])


def test_private_schema_is_guarded_and_stable_on_reopen(tmp_path: Path, private_comms) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as public:
        assert "claim_batch_receipts" not in _tables(public)
        assert "cohort_delivery_receipts" not in _tables(public)
    path = CoordinationStore.path_in(private_comms.root)
    with Coordination(str(path)) as store:
        before = _tables(store)
        assert {"claim_batch_receipts", "cohort_delivery_receipts"} <= before
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    with Coordination(str(path)) as reopened:
        assert _tables(reopened) == before
        assert reopened.session._connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert reopened.session._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not CohortTable.schema_differs(reopened.session._connection)


def test_zero_member_zero_claim_cohort_is_durable_without_fabricated_claim(private_comms) -> None:
    path = CoordinationStore.path_in(private_comms.root)
    with Coordination(str(path)) as store:
        with store.session.writing() as db:
            _receipt(db, n=0, k=0)
            _seal(db)
        assert store.session._connection.execute("SELECT sealed FROM claim_batch_receipts").fetchone()[0] == 1
        assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0
    with Coordination(str(path)) as reopened:
        assert reopened.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 1


def test_seal_rejects_observer_with_preexisting_singleton_claim(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            _second_assignment(db)
        with pytest.raises(sqlite3.IntegrityError), store.session.writing() as db:
            _receipt(db, n=2, k=1)
            _selected_and_observer(db)
            _seal(db)
        db = store.session._connection
        assert db.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM cohort_delivery_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 2


def test_selected_and_observer_receipts_are_distinct_immutable_rows(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=2, k=1)
            _selected_and_observer(db)
            _seal(db)
        db = store.session._connection
        assert [tuple(row) for row in db.execute(
            "SELECT ordinal,recipient_lookup,claim_id FROM cohort_delivery_receipts ORDER BY ordinal"
        )] == [(0, A, CLAIM_A), (1, B, None)]
        assert db.execute("SELECT COUNT(*) FROM claim_batch_members").fetchone()[0] == 1
        for row in CohortDeliveries.select(db, order_by=("ordinal",)):
            assert FieldCodec.decode(CohortDeliveries, FieldCodec.encode(row)) == row
        for statement in (
            "UPDATE claim_batch_receipts SET accepted_at_ms=101",
            "DELETE FROM claim_batch_receipts",
            "UPDATE cohort_delivery_receipts SET canonical_thread='Other'",
            "DELETE FROM claim_batch_members",
            f"UPDATE claim_batch_members SET recipient_lookup='{B}'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)


def test_seal_rejects_missing_rows_and_rolls_back_entire_cohort(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with pytest.raises(sqlite3.IntegrityError), store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=2, k=1)
            _member(db)
            _delivery(db, ordinal=0, lookup=A, name="Alice", assignment_id=CLAIM_A)
            _seal(db)
        db = store.session._connection
        for table in ("claim_batch_receipts", "wake_claims", "participants"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError), store.session.writing() as db:
            _receipt(db, n=0, k=0)
            _seal(db)
            db.execute(
                "INSERT INTO claim_batch_receipts SELECT ?,wire_seq+1,message_id,exact_target,"
                "envelope_digest,audience_digest,decisions_digest,member_count,claim_count,"
                "manifest_codec,resolver_version,policy_version,accepted_at_ms,1 "
                "FROM claim_batch_receipts", ("e" * 32,),
            )


def test_foreign_claim_lookup_and_delivery_identity_fail_before_seal(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=2, k=1)
            for assignment_id, lookup in (("missing", A), (CLAIM_A, B)):
                with pytest.raises(sqlite3.IntegrityError):
                    _member(db, assignment_id=assignment_id, lookup=lookup)
            _member(db)
            for lookup, name, assignment_id in (
                (B, "Bob", CLAIM_A), (A, "Mallory", CLAIM_A),
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    _delivery(db, ordinal=0, lookup=lookup, name=name, assignment_id=assignment_id)
            _delivery(db, ordinal=0, lookup=A, name="Alice", assignment_id=CLAIM_A)
            for lookup, name in ((A, "Other"), (B, "Alice")):
                with pytest.raises(sqlite3.IntegrityError):
                    _delivery(db, ordinal=1, lookup=lookup, name=name, assignment_id=None)
            _delivery(db, ordinal=1, lookup=B, name="Bob", assignment_id=None)
            _seal(db)


@pytest.mark.parametrize(
    "field,override",
    [("message_id", "wrong"), ("policy_version", "WRONG"), ("accepted_at_ms", 999)],
)
def test_seal_cannot_adopt_claim_with_conflicting_immutable_acceptance(
    private_comms, field: str, override: str | int,
) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with pytest.raises(sqlite3.IntegrityError), store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=1, k=1, **{field: override})
            _member(db)
            _delivery(db, ordinal=0, lookup=A, name="Alice", assignment_id=CLAIM_A)
            _seal(db)
        db = store.session._connection
        assert db.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 0


def test_one_claim_cannot_be_sealed_by_two_wire_roots(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=1, k=1)
            _member(db)
            _delivery(db, ordinal=0, lookup=A, name="Alice", assignment_id=CLAIM_A)
            _seal(db)
        with pytest.raises(sqlite3.IntegrityError), store.session.writing() as db:
            _receipt(db, n=1, k=1, root="b" * 32)
            _member(db, root="b" * 32)
        db = store.session._connection
        assert db.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM claim_batch_members").fetchone()[0] == 1


@pytest.mark.parametrize("selected", (False, True))
@pytest.mark.parametrize("stored_kind", ("selected", "unmentioned_observer", "other", None))
def test_delivery_kind_carry_preserves_consistent_rows_and_refuses_disagreement(
    private_comms, tmp_path: Path, selected: bool, stored_kind: str | None,
) -> None:
    from cutover.carry_cohort_delivery_kind import CohortDeliveryKind
    from agent_comms.errors import RelationViolationError
    from agent_comms.locked_store import LiveFilesRequest

    path = CoordinationStore.path_in(private_comms.root)
    with Coordination(str(path)) as store:
        with store.session.writing() as db:
            # The retired format is input data, not a second production declaration.
            db.execute("ALTER TABLE cohort_delivery_receipts ADD COLUMN kind TEXT")
            if selected:
                _assignment(db)
                _receipt(db, n=1, k=1)
                _member(db)
            else:
                Participants(participant_lookup=A, committed=True).insert(db)
                _receipt(db, n=1, k=0)
            db.execute(
                "INSERT INTO cohort_delivery_receipts "
                "(wire_root_id,wire_seq,ordinal,recipient_lookup,canonical_thread,claim_id,kind) "
                "VALUES (?,?,0,?,?,?,?)",
                (ROOT, SEQ, A, "Alice", CLAIM_A if selected else None, stored_kind),
            )
            _seal(db)
    before = path.read_bytes()
    step = CohortDeliveryKind(LiveFilesRequest(private_comms.root), tmp_path / "spill")
    consistent = stored_kind == ("selected" if selected else "unmentioned_observer")
    if not consistent:
        for apply in (False, True):
            with pytest.raises(RelationViolationError, match="inconsistent with its claim"):
                step.carry(apply=apply)
            assert path.read_bytes() == before
        return
    assert step.carry(apply=False).changed == 1
    assert path.read_bytes() == before
    assert step.carry(apply=True).changed == 1
    assert step.carry(apply=True).changed == 0
    with Coordination(str(path)) as store:
        db = store.session._connection
        assert [tuple(row) for row in db.execute(
            "SELECT claim_id FROM cohort_delivery_receipts"
        )] == [(CLAIM_A if selected else None,)]
        assert not db.execute("PRAGMA foreign_key_check").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM cohort_delivery_receipts")
