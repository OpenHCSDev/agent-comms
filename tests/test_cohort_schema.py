"""Cohort rows live in the guarded private root and enforce their declared relations."""

from __future__ import annotations

import os
import sqlite3
from contextlib import nullcontext
from pathlib import Path

import pytest

from bus_record_fixture import OWNER_LOOKUP, RECIPIENT_LOOKUP, SENDER_LOOKUP

from agent_comms.cohort_schema import CohortTable
from agent_comms.field_codec import ThreadLookup
from agent_comms.coordination_tables.participants import Participants
from agent_comms.coordinator import Coordination
from agent_comms.coordination_database import CoordinationStore

A, B, C = SENDER_LOOKUP, RECIPIENT_LOOKUP, OWNER_LOOKUP
ROOT = "a" * 32
SEQ = 7


def _tables(store: Coordination) -> set[str]:
    return {
        str(row[0])
        for row in store.session._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _receipt(
    db: sqlite3.Connection,
    *,
    n: int,
    k: int,
    root: str = ROOT,
    message_id: str = "msg",
    policy_version: str = "p1",
    accepted_at_ms: int = 100,
) -> None:
    db.execute(
        "INSERT INTO claim_batch_receipts (wire_root_id,wire_seq,message_id,exact_target,"
        "envelope_digest,audience_digest,decisions_digest,member_count,claim_count,"
        "manifest_codec,resolver_version,policy_version,accepted_at_ms,sealed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (
            root,
            SEQ,
            message_id,
            "#team",
            "b" * 64,
            "c" * 64,
            "d" * 64,
            n,
            k,
            "v1",
            "r1",
            policy_version,
            accepted_at_ms,
        ),
    )


def _assignment(db: sqlite3.Connection) -> None:
    Participants(participant_lookup=A, committed=True).insert(db)
    Participants(participant_lookup=B, committed=True).insert(db)
    db.execute(
        (
            "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
            "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
            f"updated_at_ms,revision) VALUES ('claim-a','Alice','{A}',7,'msg',json_object('k"
            "ind','triage_pending'),'collective','r1','p1',100,100,1)"
        )
    )


def _member(db: sqlite3.Connection) -> None:
    db.execute(
        f"INSERT INTO claim_batch_members VALUES (?,?,0,'claim-a','{A}')",
        (ROOT, SEQ),
    )


def _delivery(
    db: sqlite3.Connection,
    *,
    ordinal: int,
    lookup: ThreadLookup,
    name: str,
    kind: str,
    assignment_id: str | None,
) -> None:
    db.execute(
        "INSERT INTO cohort_delivery_receipts VALUES (?,?,?,?,?,?,?)",
        (ROOT, SEQ, ordinal, lookup, name, kind, assignment_id),
    )


def _seal(db: sqlite3.Connection) -> None:
    db.execute(
        "UPDATE claim_batch_receipts SET sealed=1 WHERE wire_root_id=? AND wire_seq=?",
        (ROOT, SEQ),
    )


def _seal_two_selected_with_observer(db: sqlite3.Connection, *, reverse_claims: bool) -> None:
    _assignment(db)
    Participants(participant_lookup=C, committed=True).insert(db)
    db.execute(
        (
            "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
            "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
            f"updated_at_ms,revision) SELECT 'claim-b','Bob','{B}',wire_seq,message_id,lifec"
            "ycle,audience,resolver_version,policy_version,accepted_at_ms,updated_at_ms,r"
            "evision FROM wake_claims WHERE assignment_id='claim-a'"
        )
    )
    _receipt(db, n=3, k=2)
    ordered = (
        (("claim-b", B), ("claim-a", A))
        if reverse_claims
        else (("claim-a", A), ("claim-b", B))
    )
    for ordinal, (assignment_id, lookup) in enumerate(ordered):
        db.execute(
            "INSERT INTO claim_batch_members VALUES (?,?,?,?,?)",
            (ROOT, SEQ, ordinal, assignment_id, lookup),
        )
    _delivery(db, ordinal=0, lookup=A, name="Alice", kind="selected", assignment_id="claim-a")
    _delivery(
        db, ordinal=1, lookup=C, name="Cara", kind="unmentioned_observer", assignment_id=None
    )
    _delivery(db, ordinal=2, lookup=B, name="Bob", kind="selected", assignment_id="claim-b")
    _seal(db)


@pytest.mark.parametrize("reverse_claims", (False, True))
def test_selected_claims_follow_ordered_n_subsequence_not_only_set_membership(
    private_comms, reverse_claims: bool,
) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with (
            pytest.raises(sqlite3.IntegrityError) if reverse_claims else nullcontext(),
            store.session.writing() as db,
        ):
            _seal_two_selected_with_observer(db, reverse_claims=reverse_claims)
        if reverse_claims:
            count = store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            assert count == 0
        else:
            sealed = store.session._connection.execute("SELECT sealed FROM claim_batch_receipts").fetchone()[0]
            assert sealed == 1


def test_private_schema_is_guarded_and_stable_on_reopen(tmp_path: Path, private_comms) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as public:
        assert "claim_batch_receipts" not in _tables(public)
        assert "cohort_delivery_receipts" not in _tables(public)
    path = CoordinationStore.path_in(private_comms.root)
    with Coordination(str(path)) as store:
        before = set(_tables(store))
        assert {"claim_batch_receipts", "cohort_delivery_receipts"} <= before
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    # Windows' st_mode does not attest NTFS ACL protection.
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
        assert (
            store.session._connection.execute("SELECT sealed FROM claim_batch_receipts").fetchone()[0] == 1
        )
        assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0
    with Coordination(str(path)) as reopened:
        assert (
            reopened.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 1
        )


def test_seal_rejects_observer_with_preexisting_legacy_singleton_claim(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            db.execute(
                (
                    "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
                    "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
                    "updated_at_ms,revision) "
                    f"SELECT 'legacy-b','Bob','{B}',wire_seq,message_id,life"
                    "cycle,audience,resolver_version,policy_version,accepted_at_ms,updated_at_ms,"
                    "revision FROM wake_claims WHERE assignment_id='claim-a'"
                )
            )
        with (
            pytest.raises(sqlite3.IntegrityError),
            store.session.writing() as db,
        ):
            _receipt(db, n=2, k=1)
            _member(db)
            _delivery(
                db, ordinal=0, lookup=A, name="Alice", kind="selected", assignment_id="claim-a"
            )
            _delivery(
                db,
                ordinal=1,
                lookup=B,
                name="Bob",
                kind="unmentioned_observer",
                assignment_id=None,
            )
            _seal(db)
        db = store.session._connection
        assert db.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM cohort_delivery_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 2


def test_selected_and_observer_receipts_are_distinct_immutable_rows(private_comms) -> None:
    from agent_comms.cohort_schema import CohortDeliveries
    from agent_comms.field_codec import FieldCodec

    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=2, k=1)
            _member(db)
            _delivery(
                db, ordinal=0, lookup=A, name="Alice", kind="selected", assignment_id="claim-a"
            )
            _delivery(
                db,
                ordinal=1,
                lookup=B,
                name="Bob",
                kind="unmentioned_observer",
                assignment_id=None,
            )
            _seal(db)
        db = store.session._connection
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT ordinal,recipient_lookup,kind,claim_id "
                "FROM cohort_delivery_receipts ORDER BY ordinal"
            )
        ] == [(0, A, "selected", "claim-a"), (1, B, "unmentioned_observer", None)]
        assert db.execute("SELECT COUNT(*) FROM claim_batch_members").fetchone()[0] == 1
        for row in CohortDeliveries.select(db, order_by=("ordinal",)):
            encoded = FieldCodec.encode(row)
            assert encoded["row"] == CohortDeliveries.declared_name
            assert encoded["kind"] == row.kind
            assert FieldCodec.decode(CohortDeliveries, encoded) == row
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
        with pytest.raises(sqlite3.IntegrityError):  # noqa: SIM117
            with store.session.writing() as db:
                _assignment(db)
                _receipt(db, n=2, k=1)
                _member(db)
                _delivery(
                    db,
                    ordinal=0,
                    lookup=A,
                    name="Alice",
                    kind="selected",
                    assignment_id="claim-a",
                )
                _seal(db)
        assert (
            store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 0
        )
        assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0
        assert store.session._connection.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):  # noqa: SIM117
            with store.session.writing() as db:
                _receipt(db, n=0, k=0)
                db.execute("UPDATE claim_batch_receipts SET sealed=1")
                db.execute(
                    "INSERT INTO claim_batch_receipts "
                    "(wire_root_id,wire_seq,message_id,exact_target,envelope_digest,"
                    "audience_digest,decisions_digest,member_count,claim_count,manifest_codec,"
                    "resolver_version,policy_version,accepted_at_ms,sealed) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (
                        "e" * 32,
                        8,
                        "msg",
                        "#team",
                        "b" * 64,
                        "c" * 64,
                        "d" * 64,
                        0,
                        0,
                        "v1",
                        "r1",
                        "p1",
                        100,
                    ),
                )


def test_foreign_claim_lookup_and_kind_shape_fail_before_seal(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=2, k=1)
            for assignment_id, lookup in (("missing", A), ("claim-a", B)):
                with pytest.raises(
                    sqlite3.IntegrityError
                ):
                    db.execute(
                        "INSERT INTO claim_batch_members VALUES (?,?,0,?,?)",
                        (ROOT, SEQ, assignment_id, lookup),
                    )
            _member(db)
            for kind, assignment_id in (("selected", None), ("unmentioned_observer", "claim-a")):
                with pytest.raises(sqlite3.IntegrityError):
                    _delivery(
                        db,
                        ordinal=0,
                        lookup=A,
                        name="Alice",
                        kind=kind,
                        assignment_id=assignment_id,
                    )
            with pytest.raises(sqlite3.IntegrityError):
                _delivery(
                    db, ordinal=0, lookup=B, name="Bob", kind="selected", assignment_id="claim-a"
                )
            with pytest.raises(sqlite3.IntegrityError):
                _delivery(
                    db,
                    ordinal=0,
                    lookup=A,
                    name="Mallory",
                    kind="selected",
                    assignment_id="claim-a",
                )
            _delivery(
                db, ordinal=0, lookup=A, name="Alice", kind="selected", assignment_id="claim-a"
            )
            with pytest.raises(sqlite3.IntegrityError):
                _delivery(
                    db,
                    ordinal=1,
                    lookup=A,
                    name="Other",
                    kind="unmentioned_observer",
                    assignment_id=None,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _delivery(
                    db,
                    ordinal=1,
                    lookup=B,
                    name="Alice",
                    kind="unmentioned_observer",
                    assignment_id=None,
                )
            _delivery(
                db,
                ordinal=1,
                lookup=B,
                name="Bob",
                kind="unmentioned_observer",
                assignment_id=None,
            )
            _seal(db)


@pytest.mark.parametrize(
    "field,override",
    [("message_id", "wrong"), ("policy_version", "WRONG"), ("accepted_at_ms", 999)],
)
def test_seal_cannot_adopt_claim_with_conflicting_immutable_acceptance(
    private_comms, field: str, override: str | int
) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with (
            pytest.raises(sqlite3.IntegrityError),
            store.session.writing() as db,
        ):
            _assignment(db)
            _receipt(db, n=1, k=1, **{field: override})
            _member(db)
            _delivery(
                db, ordinal=0, lookup=A, name="Alice", kind="selected", assignment_id="claim-a"
            )
            _seal(db)
        assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0
        assert (
            store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 0
        )


def test_one_claim_cannot_be_sealed_by_two_wire_roots(private_comms) -> None:
    with Coordination(str(CoordinationStore.path_in(private_comms.root))) as store:
        with store.session.writing() as db:
            _assignment(db)
            _receipt(db, n=1, k=1)
            _member(db)
            _delivery(
                db, ordinal=0, lookup=A, name="Alice", kind="selected", assignment_id="claim-a"
            )
            _seal(db)
        with pytest.raises(sqlite3.IntegrityError), store.session.writing() as db:
            _receipt(db, n=1, k=1, root="b" * 32)
            db.execute(
                f"INSERT INTO claim_batch_members VALUES (?,?,0,'claim-a','{A}')",
                ("b" * 32, SEQ),
            )
        assert (
            store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 1
        )
        db = store.session._connection
        member_count = db.execute("SELECT COUNT(*) FROM claim_batch_members").fetchone()[0]
        assert member_count == 1




