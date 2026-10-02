"""The presentation reader must not gain coordinator or filesystem write authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.publications import canonical_publication_key
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType
from agent_comms.owner_lifecycle import OwnerLifecycle
from agent_comms.recovery_projection import (
    AvailableRecoveryProjection,
    UnavailableRecoveryProjection,
    read_recovery_projection,
)


def register(db: sqlite3.Connection, lookup: str, name: str) -> None:
    db.execute("INSERT INTO participants VALUES (?, ?, 1)", (lookup, name))
    db.execute("INSERT INTO owner_generations VALUES (?, ?, 1)", (lookup, name))
    db.execute(
        "INSERT INTO current_executions "
        "(owner_lookup,execution_id,attempt_ordinal,pointer_revision) "
        "VALUES (?,NULL,NULL,0)",
        (lookup,),
    )


def pending(db: sqlite3.Connection, lookup: str, name: str, execution: str) -> None:
    db.execute(
        (
            "INSERT INTO executions (execution_id,origin,lifecycle,exact_target,owner_thr"
            "ead,owner_lookup,revision,max_attempts,reason_code,created_at_ms,updated_at_"
            "ms) VALUES (?,'acp',json_object('kind','pending'),NULL,?,?,1,2,NULL,5,5)"
        ),
        (execution, name, lookup),
    )


@pytest.fixture
def private_db(tmp_path: Path):
    path = tmp_path / "coordination.sqlite3"
    with CoordinationStore(path) as store:
        db = store._connection
        register(db, "a", "Alice")
        register(db, "b", "Bob")
        pending(db, "a", "Alice", "alice-execution")
        pending(db, "b", "Bob", "bob-execution")
    return path


def view(path: Path, lookup: str = "a", name: str = "Alice"):
    return read_recovery_projection(path, owner_lookup=lookup, owner_thread=name)


@pytest.fixture
def active_db(private_db: Path) -> Path:
    with sqlite3.connect(private_db) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            (
                "INSERT INTO attempts (execution_id,attempt_ordinal,owner_lookup,owner_thread,owner_generation,owner_token_digest,lifecycle,revision,last_progress_at_ms,reason_code,created_at_ms,updated_at_ms) VALUES ('alice-execution',1,'a','Alice',1,'private-fence',json_object('kind','prompt_starting','lease_expires_at_ms',500,'backend_done',json('false'),'process_dead',json('false')),1,NULL,NULL,5,5)"
            )
        )
        db.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
                "nal',1),revision=2 WHERE execution_id='alice-execution'"
            )
        )
        db.execute(
            "UPDATE current_executions SET execution_id='alice-execution',attempt_ordinal=1,"
            "pointer_revision=1 WHERE owner_lookup='a'"
        )
        db.execute("COMMIT")
    return private_db


def test_owner_scope_and_deterministic_single_execution(private_db: Path):
    first = view(private_db)
    assert isinstance(first, AvailableRecoveryProjection)
    assert FieldCodec.encode(first)["current"] == {
        "status": "pending",
        "origin": "acp",
        "isCurrent": False,
        "attempt": None,
        "canRetry": False,
        "publication": None,
    }
    assert first.last_recovery is None and first.connectivity is None
    assert "bob-execution" not in json.dumps(FieldCodec.encode(first))
    assert isinstance(view(private_db, "b", "Bob"), AvailableRecoveryProjection)
    assert view(private_db, "a", "Bob") == UnavailableRecoveryProjection("unknown_owner")
    assert view(private_db, "alias", "Alice") == UnavailableRecoveryProjection("unknown_owner")
    # A provided owner tuple is only canonical scoping, NOT viewer authentication.
    with sqlite3.connect(private_db) as db:
        pending(db, "a", "Alice", "alice-later")
        db.execute(
            "UPDATE executions SET revision=2,updated_at_ms=10 WHERE execution_id='alice-later'"
        )
    assert isinstance(view(private_db), AvailableRecoveryProjection)
    # The DTO never carries an execution ID or a row count of unbounded history.
    assert "alice-later" not in json.dumps(FieldCodec.encode(view(private_db)))


def test_corrupt_missing_schema_busy_and_nonregular_fail_closed(tmp_path: Path, private_db: Path):
    missing = tmp_path / "uncreated" / "coordination.sqlite3"
    assert view(missing) == UnavailableRecoveryProjection("missing")
    assert not missing.parent.exists()
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not a database")
    assert view(corrupt) == UnavailableRecoveryProjection("invalid_store")
    assert view(tmp_path) == UnavailableRecoveryProjection("invalid_store")
    with sqlite3.connect(private_db) as db:
        db.execute("PRAGMA user_version = 991")
    assert view(private_db) == UnavailableRecoveryProjection("unsupported_schema")
    with sqlite3.connect(private_db) as db:
        db.execute("PRAGMA user_version = 2")
        db.execute("DROP TRIGGER schema_meta_update_frozen")
        db.execute("UPDATE schema_meta SET snapshot_version=999 WHERE singleton=1")
    assert view(private_db) == UnavailableRecoveryProjection("unsupported_schema")
    with sqlite3.connect(private_db) as db:
        db.execute("UPDATE schema_meta SET snapshot_version=2 WHERE singleton=1")
        db.commit()
        db.execute("BEGIN EXCLUSIVE")
        try:
            assert view(private_db) == UnavailableRecoveryProjection("busy")
        finally:
            db.rollback()


def test_sqlite_read_transaction_cannot_mix_owner_rows(private_db: Path, monkeypatch):
    import agent_comms.recovery_projection as projection

    pinned = threading.Event()
    update_started = threading.Event()
    release = threading.Event()
    committed = threading.Event()
    original = projection.RecoverySelection.project

    def mid_transaction(connection, lookup, thread):
        # The first SELECT pins SQLite's rollback-journal reader snapshot.
        assert (
            connection.execute(
                "SELECT status FROM executions WHERE execution_id='alice-execution'"
            ).fetchone()[0]
            == "pending"
        )
        pinned.set()
        assert update_started.wait(2)
        assert release.wait(2)
        return original(connection, lookup, thread)

    monkeypatch.setattr(projection.RecoverySelection, "project", staticmethod(mid_transaction))
    result = []
    reader = threading.Thread(target=lambda: result.append(view(private_db)))

    def writer():
        assert pinned.wait(2)
        with sqlite3.connect(private_db, timeout=3) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                (
                    "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'), revis"
                    "ion=2, updated_at_ms=11 WHERE execution_id='alice-execution'"
                )
            )
            update_started.set()
        committed.set()

    changing = threading.Thread(target=writer)
    reader.start()
    changing.start()
    try:
        assert update_started.wait(2)
        assert not committed.wait(0.1)  # The writer cannot commit half a reader snapshot.
    finally:
        release.set()
        reader.join(4)
        changing.join(4)
    assert not reader.is_alive() and not changing.is_alive()
    assert committed.is_set()
    assert isinstance(result[0], AvailableRecoveryProjection)
    assert result[0].current is not None and result[0].current.status.declared_name == "pending"
    monkeypatch.setattr(projection.RecoverySelection, "project", staticmethod(original))
    latest = view(private_db)
    assert isinstance(latest, AvailableRecoveryProjection)
    assert latest.current is not None and latest.current.status.declared_name == "failed"


def test_current_pointer_and_offline_compaction_phase_are_evidence_not_lifecycle(
    private_db: Path,
):
    with sqlite3.connect(private_db) as db:
        db.execute("BEGIN IMMEDIATE")
        pending(db, "a", "Alice", "later-pending")
        db.execute(
            "UPDATE executions SET revision=2,updated_at_ms=100 WHERE execution_id='later-pending'"
        )
        db.execute(
            (
                "INSERT INTO attempts (execution_id,attempt_ordinal,owner_lookup,owner_thread,owner_generation,owner_token_digest,lifecycle,revision,last_progress_at_ms,reason_code,created_at_ms,updated_at_ms) VALUES ('alice-execution',1,'a','Alice',1,'private-fence',json_object('kind','prompt_starting','lease_expires_at_ms',500,'backend_done',json('false'),'process_dead',json('false')),1,NULL,NULL,5,5)"
            )
        )
        db.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
                "nal',1),revision=2 WHERE execution_id='alice-execution'"
            )
        )
        db.execute(
            "UPDATE current_executions SET execution_id='alice-execution',attempt_ordinal=1,"
            "pointer_revision=1 WHERE owner_lookup='a'"
        )
        db.execute(
            "INSERT INTO connectivity VALUES ('alice-execution','offline','disconnected',1,50)"
        )
        db.execute("COMMIT")
        db.execute(
            (
                "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.kind','prompt_accepted')"
                ",revision=2 WHERE execution_id='alice-execution'"
            )
        )
        db.execute(
            (
                "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.kind','model_running'),r"
                "evision=3 WHERE execution_id='alice-execution'"
            )
        )
        db.execute(
            (
                "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.kind','compaction','$.ba"
                "ckend_done',json('true')),revision=4 WHERE execution_id='alice-execution'"
            )
        )
    result = view(private_db)
    assert isinstance(result, AvailableRecoveryProjection)
    assert result.current is not None and result.current.is_current
    assert result.current.status.declared_name == "active"
    assert result.current.attempt is not None
    assert result.current.attempt.phase.declared_name == "compaction"
    assert result.current.attempt.backend_done and not result.current.attempt.backend_process_exited
    assert FieldCodec.encode(result)["current"]["attempt"]["backendProcessExited"] is False
    assert "processDead" not in json.dumps(FieldCodec.encode(result))
    assert not result.current.can_retry
    assert result.connectivity is not None and result.connectivity.owner.value == "offline"
    assert result.connectivity.acp_client.value == "disconnected"
    assert result.last_recovery is None  # Phase alone is not a recovery audit.
    wire = json.dumps(FieldCodec.encode(result))
    assert "later-pending" not in wire and "private-fence" not in wire
    assert "startedAt" not in wire and "completedAt" not in wire
    assert "contextUsed" not in wire and "compactionStartedAt" not in wire
    # Pi RPC child exit and registry-owner connectivity are independent facts.
    with sqlite3.connect(private_db) as db:
        db.execute(
            (
                "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.process_dead',json('true"
                "')),revision=5 WHERE execution_id='alice-execution'"
            )
        )
        db.execute(
            "UPDATE connectivity SET owner='connected',revision=2,"
            "observed_at_ms=51 WHERE execution_id='alice-execution'"
        )
    exited = view(private_db)
    assert isinstance(exited, AvailableRecoveryProjection)
    assert exited.current is not None and exited.current.attempt is not None
    assert exited.current.attempt.backend_process_exited
    assert exited.connectivity is not None and exited.connectivity.owner.value == "connected"
    assert not exited.current.can_retry  # `settled` or child exit cannot terminalize.


@pytest.mark.parametrize(
    ("column", "invalid"),
    [("backend_done", 2), ("backend_done", -1), ("process_dead", 2), ("process_dead", -1)],
)
def test_invalid_backend_boolean_never_becomes_truthy(active_db: Path, column: str, invalid: int):
    with sqlite3.connect(active_db) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            f"UPDATE attempts SET lifecycle=json_set(lifecycle, '$.{column}',?),revision=revision+1 "
            "WHERE execution_id='alice-execution'",
            (invalid,),
        )
    assert view(active_db) == UnavailableRecoveryProjection("invalid_store")


@pytest.mark.parametrize(
    ("column", "invalid"),
    [("attempt", 0), ("elapsed_ms", -1), ("observed_at_ms", -1)],
)
def test_invalid_audit_number_fails_closed(active_db: Path, column: str, invalid: int):
    fields = {"attempt": 1, "elapsed_ms": 0, "observed_at_ms": 0}
    fields[column] = invalid
    with sqlite3.connect(active_db) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "INSERT INTO recovery_audit "
            "(execution_id,kind,reason_code,sanitized_detail,attempt,elapsed_ms,observed_at_ms) "
            "VALUES ('alice-execution','retrying','r',NULL,?,?,?)",
            (fields["attempt"], fields["elapsed_ms"], fields["observed_at_ms"]),
        )
    assert view(active_db) == UnavailableRecoveryProjection("invalid_store")


def test_invalid_connectivity_timestamp_fails_closed(active_db: Path):
    with sqlite3.connect(active_db) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "INSERT INTO connectivity VALUES ('alice-execution','connected','disconnected',1,-1)"
        )
    assert view(active_db) == UnavailableRecoveryProjection("invalid_store")


def test_invalid_current_pointer_ordinal_fails_closed(active_db: Path):
    with sqlite3.connect(active_db) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "UPDATE current_executions SET attempt_ordinal=-1,pointer_revision=2 "
            "WHERE owner_lookup='a'"
        )
    assert view(active_db) == UnavailableRecoveryProjection("invalid_store")


def test_cleared_pointer_with_active_execution_fails_closed(active_db: Path):
    with sqlite3.connect(active_db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
            "pointer_revision=2 WHERE owner_lookup='a'"
        )
    assert view(active_db) == UnavailableRecoveryProjection("invalid_store")


def test_other_active_execution_disagrees_with_pointer(active_db: Path):
    with sqlite3.connect(active_db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        pending(db, "a", "Alice", "other-active")
        db.execute(
            (
                "INSERT INTO attempts (execution_id,attempt_ordinal,owner_lookup,owner_thread,owner_generation,owner_token_digest,lifecycle,revision,last_progress_at_ms,reason_code,created_at_ms,updated_at_ms) VALUES ('other-active',1,'a','Alice',1,'other-token',json_object('kind','prompt_starting','lease_expires_at_ms',500,'backend_done',json('false'),'process_dead',json('false')),1,NULL,NULL,5,5)"
            )
        )
        db.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
                "nal',1),revision=2 WHERE execution_id='other-active'"
            )
        )
        db.execute("COMMIT")
    assert view(active_db) == UnavailableRecoveryProjection("invalid_store")


def test_invalid_retry_view_and_exact_receipt_domain(active_db: Path):
    # The declared retry view always yields 0/1 under its current schema.
    # A malformed stored view must not turn 2 into a truthy retry decision.
    with sqlite3.connect(active_db) as db:
        db.execute("DROP VIEW retry_disposition_basis")
        db.execute(
            "CREATE VIEW retry_disposition_basis AS "
            "SELECT execution_id, 2 AS authorized FROM executions"
        )
    assert view(active_db) == UnavailableRecoveryProjection("invalid_store")


def test_publication_uncertain_and_recursive_privacy(tmp_path: Path):
    path = tmp_path / "coordination.sqlite3"
    with CoordinationStore(path) as store:
        db = store._connection
        register(db, "a", "Alice")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            (
                "INSERT INTO executions (execution_id,origin,lifecycle,exact_target,owner_thr"
                "ead,owner_lookup,revision,max_attempts,reason_code,created_at_ms,updated_at_"
                "ms) VALUES ('e','wire',json_object('kind','queued'),'private-target','Alice'"
                ",'a',1,2,NULL,0,0)"
            )
        )
        db.execute(
            (
                "INSERT INTO obligations (execution_id,exact_target,lifecycle,reason_code,cre"
                "ated_at_ms,updated_at_ms,revision) VALUES ('e','private-target',json_object("
                "'kind','pending'),NULL,0,0,1)"
            )
        )
        db.execute(
            (
                "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
                "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
                "updated_at_ms,revision) VALUES ('private-claim','Alice','a',1,'private-messa"
                "ge',json_object('kind','engaged','decision',json_object('kind','full','exact"
                "_target','private-target','execution_id','e')),'direct','resolver','policy',"
                "0,0,1)"
            )
        )
        db.execute("INSERT INTO execution_claims VALUES ('e','private-claim',0)")
        db.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','pending'),revis"
                "ion=2 WHERE execution_id='e'"
            )
        )
        db.execute(
            (
                "INSERT INTO attempts (execution_id,attempt_ordinal,owner_lookup,owner_thread,owner_generation,owner_token_digest,lifecycle,revision,last_progress_at_ms,reason_code,created_at_ms,updated_at_ms) VALUES ('e',1,'a','Alice',1,'private-token',json_object('kind','prompt_starting','lease_expires_at_ms',500,'backend_done',json('false'),'process_dead',json('false')),1,NULL,NULL,0,0)"
            )
        )
        db.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
                "nal',1),revision=3 WHERE execution_id='e'"
            )
        )
        db.execute(
            "UPDATE current_executions SET execution_id='e',attempt_ordinal=1,"
            "pointer_revision=1 WHERE owner_lookup='a'"
        )
        db.execute("COMMIT")
        message = Message(
            sender="Alice",
            target="private-target",
            body="private-payload",
            type=MessageType.INFO,
            timestamp=1.0,
            notice=False,
        )
        key = canonical_publication_key("e", "private-target")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "e",
                "Alice",
                "private-target",
                "info",
                0,
                1.0,
                "private-payload",
                hashlib.sha256(b"private-payload").hexdigest(),
                key,
                message.message_id,
            ),
        )
        db.execute(
            (
                "UPDATE obligations SET lifecycle=json_object('kind','publishing'),revision=2"
                " WHERE execution_id='e'"
            )
        )
        db.execute("COMMIT")
        db.execute(
            "INSERT INTO recovery_audit (execution_id,kind,reason_code,sanitized_detail,"
            "attempt,elapsed_ms,observed_at_ms) VALUES ('e','retrying',"
            "'private-reason','private-detail',1,250,100)"
        )
    result = view(path)
    assert isinstance(result, AvailableRecoveryProjection)
    assert result.current is not None and result.current.publication == "uncertain"
    rendered = json.dumps(FieldCodec.encode(result), sort_keys=True)
    for secret in (
        "private-target",
        "private-claim",
        "private-message",
        "private-payload",
        "private-reason",
        "private-detail",
        "private-token",
        key,
        message.message_id,
    ):
        assert secret not in rendered
    assert result.last_recovery is not None
    assert result.last_recovery.elapsed_ms == 250
    assert "revision" not in rendered and "compaction" not in rendered


def test_offline_owner_reader_does_not_change_db_sidecars_or_directory(private_db: Path):
    directory = private_db.parent
    before_names = {
        p.name: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
        for p in directory.iterdir()
    }
    before_directory = directory.stat().st_mtime_ns
    for _ in range(3):
        assert isinstance(view(private_db), AvailableRecoveryProjection)
    assert {
        p.name: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
        for p in directory.iterdir()
    } == before_names
    assert directory.stat().st_mtime_ns == before_directory
    assert not any(p.name.endswith(("-journal", "-wal", "-shm")) for p in directory.iterdir())


def test_wal_mode_refused_before_shared_memory_sidecar(tmp_path: Path):
    path = tmp_path / "wal.sqlite3"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("CREATE TABLE test (id INTEGER)")
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert view(path) == UnavailableRecoveryProjection("invalid_store")
    assert {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.iterdir()} == before


def test_no_owner_process_start_or_gateway_calls(private_db: Path, monkeypatch):

    def prohibited(*_args, **_kwargs):
        raise AssertionError("reader must not start an owner or mutate a Comms registry")

    monkeypatch.setattr(OwnerLifecycle, "ensure_owner", prohibited)
    monkeypatch.setattr(OwnerLifecycle, "start", prohibited)
    before_pid = os.getpid()
    assert isinstance(view(private_db), AvailableRecoveryProjection)
    assert os.getpid() == before_pid
