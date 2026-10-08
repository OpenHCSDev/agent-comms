"""The optional cohort schema is inert until an explicit private migration."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.coordination_schema import COORDINATION_SCHEMA_VERSION
from agent_comms.coordinator import Coordination

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
    db.execute("INSERT INTO participants VALUES ('a','Alice',1)")
    db.execute("INSERT INTO participants VALUES ('b','Bob',1)")
    db.execute(
        (
            "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
            "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
            "updated_at_ms,revision) VALUES ('claim-a','Alice','a',7,'msg',json_object('k"
            "ind','triage_pending'),'collective','r1','p1',100,100,1)"
        )
    )


def _member(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO claim_batch_members VALUES (?,?,0,'claim-a','a')",
        (ROOT, SEQ),
    )


def _delivery(
    db: sqlite3.Connection,
    *,
    ordinal: int,
    lookup: str,
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
    db.execute("INSERT INTO participants VALUES ('c','Cara',1)")
    db.execute(
        (
            "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
            "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
            "updated_at_ms,revision) SELECT 'claim-b','Bob','b',wire_seq,message_id,lifec"
            "ycle,audience,resolver_version,policy_version,accepted_at_ms,updated_at_ms,r"
            "evision FROM wake_claims WHERE assignment_id='claim-a'"
        )
    )
    _receipt(db, n=3, k=2)
    ordered = (
        (("claim-b", "b"), ("claim-a", "a"))
        if reverse_claims
        else (("claim-a", "a"), ("claim-b", "b"))
    )
    for ordinal, (assignment_id, lookup) in enumerate(ordered):
        db.execute(
            "INSERT INTO claim_batch_members VALUES (?,?,?,?,?)",
            (ROOT, SEQ, ordinal, assignment_id, lookup),
        )
    _delivery(db, ordinal=0, lookup="a", name="Alice", kind="selected", assignment_id="claim-a")
    _delivery(
        db, ordinal=1, lookup="c", name="Cara", kind="unmentioned_observer", assignment_id=None
    )
    _delivery(db, ordinal=2, lookup="b", name="Bob", kind="selected", assignment_id="claim-b")
    _seal(db)


def test_selected_claims_follow_ordered_n_subsequence_not_only_set_membership(
    tmp_path: Path,
) -> None:
    with Coordination(str(tmp_path / "valid.sqlite3")) as store:
        install_private_cohort_schema(store)
        with store.session.transaction() as db:
            _seal_two_selected_with_observer(db, reverse_claims=False)
        sealed = store.session._connection.execute("SELECT sealed FROM claim_batch_receipts").fetchone()[0]
        assert sealed == 1
    with Coordination(str(tmp_path / "reversed.sqlite3")) as store:
        install_private_cohort_schema(store)
        with (
            pytest.raises(sqlite3.IntegrityError, match="selected claim order"),
            store.session.transaction() as db,
        ):
            _seal_two_selected_with_observer(db, reverse_claims=True)
        count = store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
        assert count == 0


def test_schema_is_opt_in_versioned_and_idempotent_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "coordination.sqlite3"
    with Coordination(str(path)) as store:
        assert store.session.schema_version == COORDINATION_SCHEMA_VERSION
        assert "claim_batch_receipts" not in _tables(store)
        assert "cohort_delivery_receipts" not in _tables(store)
        install_private_cohort_schema(store)
        before = set(_tables(store))
        install_private_cohort_schema(store)
        assert _tables(store) == before
        assert store.session.schema_version == COORDINATION_SCHEMA_VERSION
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    # Windows' st_mode does not attest NTFS ACL protection.
    with Coordination(str(path)) as reopened:
        install_private_cohort_schema(reopened)
        assert _tables(reopened) == before
        assert reopened.session._connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert reopened.session._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            reopened.session._connection.execute("SELECT version FROM cohort_schema_meta").fetchone()[0]
            == 2
        )


def test_zero_member_zero_claim_cohort_is_durable_without_fabricated_claim(tmp_path: Path) -> None:
    path = tmp_path / "coordination.sqlite3"
    with Coordination(str(path)) as store:
        install_private_cohort_schema(store)
        with store.session.transaction() as db:
            _receipt(db, n=0, k=0)
            _seal(db)
        assert (
            store.session._connection.execute("SELECT sealed FROM claim_batch_receipts").fetchone()[0] == 1
        )
        assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0
    with Coordination(str(path)) as reopened:
        install_private_cohort_schema(reopened)
        assert (
            reopened.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 1
        )


def test_seal_rejects_observer_with_preexisting_legacy_singleton_claim(tmp_path: Path) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        with store.session.transaction() as db:
            _assignment(db)
            db.execute(
                (
                    "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
                    "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
                    "updated_at_ms,revision) SELECT 'legacy-b','Bob','b',wire_seq,message_id,life"
                    "cycle,audience,resolver_version,policy_version,accepted_at_ms,updated_at_ms,"
                    "revision FROM wake_claims WHERE assignment_id='claim-a'"
                )
            )
        with (
            pytest.raises(sqlite3.IntegrityError, match="observer has an existing wake claim"),
            store.session.transaction() as db,
        ):
            _receipt(db, n=2, k=1)
            _member(db)
            _delivery(
                db, ordinal=0, lookup="a", name="Alice", kind="selected", assignment_id="claim-a"
            )
            _delivery(
                db,
                ordinal=1,
                lookup="b",
                name="Bob",
                kind="unmentioned_observer",
                assignment_id=None,
            )
            _seal(db)
        db = store.session._connection
        assert db.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM cohort_delivery_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 2


def test_selected_and_observer_receipts_are_distinct_immutable_rows(tmp_path: Path) -> None:
    from agent_comms.cohort_schema import CohortDeliveryReceipts
    from agent_comms.field_codec import FieldCodec

    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        with store.session.transaction() as db:
            _assignment(db)
            _receipt(db, n=2, k=1)
            _member(db)
            _delivery(
                db, ordinal=0, lookup="a", name="Alice", kind="selected", assignment_id="claim-a"
            )
            _delivery(
                db,
                ordinal=1,
                lookup="b",
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
        ] == [(0, "a", "selected", "claim-a"), (1, "b", "unmentioned_observer", None)]
        assert db.execute("SELECT COUNT(*) FROM claim_batch_members").fetchone()[0] == 1
        for row in CohortDeliveryReceipts.select(db, order_by=("ordinal",)):
            encoded = FieldCodec.encode(row)
            assert encoded["row"] == CohortDeliveryReceipts.declared_name
            assert encoded["kind"] == row.kind
            assert FieldCodec.decode(CohortDeliveryReceipts, encoded) == row
        for statement in (
            "UPDATE claim_batch_receipts SET accepted_at_ms=101",
            "DELETE FROM claim_batch_receipts",
            "UPDATE cohort_delivery_receipts SET canonical_thread='Other'",
            "DELETE FROM claim_batch_members",
            "UPDATE claim_batch_members SET recipient_lookup='b'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)


def test_seal_rejects_missing_rows_and_rolls_back_entire_cohort(tmp_path: Path) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        with pytest.raises(sqlite3.IntegrityError, match="incomplete"):  # noqa: SIM117
            with store.session.transaction() as db:
                _assignment(db)
                _receipt(db, n=2, k=1)
                _member(db)
                _delivery(
                    db,
                    ordinal=0,
                    lookup="a",
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
        with pytest.raises(sqlite3.IntegrityError, match="sealed after"):  # noqa: SIM117
            with store.session.transaction() as db:
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


def test_foreign_claim_lookup_and_kind_shape_fail_before_seal(tmp_path: Path) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        with store.session.transaction() as db:
            _assignment(db)
            _receipt(db, n=2, k=1)
            for assignment_id, lookup in (("missing", "a"), ("claim-a", "b")):
                with pytest.raises(
                    sqlite3.IntegrityError, match="does not match cohort acceptance"
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
                        lookup="a",
                        name="Alice",
                        kind=kind,
                        assignment_id=assignment_id,
                    )
            with pytest.raises(sqlite3.IntegrityError, match="matching claim member"):
                _delivery(
                    db, ordinal=0, lookup="b", name="Bob", kind="selected", assignment_id="claim-a"
                )
            with pytest.raises(sqlite3.IntegrityError, match="matching claim member"):
                _delivery(
                    db,
                    ordinal=0,
                    lookup="a",
                    name="Mallory",
                    kind="selected",
                    assignment_id="claim-a",
                )
            _delivery(
                db, ordinal=0, lookup="a", name="Alice", kind="selected", assignment_id="claim-a"
            )
            with pytest.raises(sqlite3.IntegrityError):
                _delivery(
                    db,
                    ordinal=1,
                    lookup="a",
                    name="Other",
                    kind="unmentioned_observer",
                    assignment_id=None,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _delivery(
                    db,
                    ordinal=1,
                    lookup="b",
                    name="Alice",
                    kind="unmentioned_observer",
                    assignment_id=None,
                )
            _delivery(
                db,
                ordinal=1,
                lookup="b",
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
    tmp_path: Path, field: str, override: str | int
) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        with (
            pytest.raises(sqlite3.IntegrityError, match="does not match cohort acceptance"),
            store.session.transaction() as db,
        ):
            _assignment(db)
            _receipt(db, n=1, k=1, **{field: override})
            _member(db)
            _delivery(
                db, ordinal=0, lookup="a", name="Alice", kind="selected", assignment_id="claim-a"
            )
            _seal(db)
        assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0
        assert (
            store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 0
        )


def test_one_claim_cannot_be_sealed_by_two_wire_roots(tmp_path: Path) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        with store.session.transaction() as db:
            _assignment(db)
            _receipt(db, n=1, k=1)
            _member(db)
            _delivery(
                db, ordinal=0, lookup="a", name="Alice", kind="selected", assignment_id="claim-a"
            )
            _seal(db)
        with pytest.raises(sqlite3.IntegrityError), store.session.transaction() as db:
            _receipt(db, n=1, k=1, root="b" * 32)
            db.execute(
                "INSERT INTO claim_batch_members VALUES (?,?,0,'claim-a','a')",
                ("b" * 32, SEQ),
            )
        assert (
            store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 1
        )
        db = store.session._connection
        member_count = db.execute("SELECT COUNT(*) FROM claim_batch_members").fetchone()[0]
        assert member_count == 1


@pytest.mark.parametrize("problem", ["partial", "version", "drift"])
def test_reopen_rejects_partial_unsupported_or_drifted_schema(tmp_path: Path, problem: str) -> None:
    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        if problem == "partial":
            store.session._connection.execute("CREATE TABLE cohort_schema_meta (x INTEGER)")
        else:
            install_private_cohort_schema(store)
            if problem == "version":
                store.session._connection.execute("DROP TRIGGER cohort_meta_update_guard")
                store.session._connection.execute("PRAGMA ignore_check_constraints=ON")
                try:
                    store.session._connection.execute("UPDATE cohort_schema_meta SET version=999")
                finally:
                    store.session._connection.execute("PRAGMA ignore_check_constraints=OFF")
            else:
                store.session._connection.execute("DROP INDEX cohort_delivery_receipts_0_idx")
        before = set(_tables(store))
        with pytest.raises(SchemaVersionError):
            install_private_cohort_schema(store)
        assert _tables(store) == before
        assert not store.session._connection.in_transaction


def test_two_process_like_connections_serialize_explicit_migration(tmp_path: Path) -> None:
    path = str(tmp_path / "coordination.sqlite3")
    with Coordination(path):
        pass
    barrier = Barrier(2)

    def migrate() -> int:
        with Coordination(path) as store:
            barrier.wait(timeout=5)
            install_private_cohort_schema(store)
            return store.session._connection.execute("SELECT version FROM cohort_schema_meta").fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(migrate)
        right = pool.submit(migrate)
        assert (left.result(timeout=10), right.result(timeout=10)) == (2, 2)
