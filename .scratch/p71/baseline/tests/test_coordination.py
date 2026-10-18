"""Slice-1 store_files, direct-SQL authority and crash/reopen shape tests."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_comms.assignment_states import (
    CompletedAssignment,
    EngagedAssignment,
)
from agent_comms.attempt_states import (
    AbortingAttempt,
    AttemptFailedAttempt,
    ModelRunningAttempt,
    PromptAcceptedAttempt,
    PromptStartingAttempt,
    SettlingAttempt,
    RetryingAttempt,
    SucceededAttempt,
)
from agent_comms.coordination_database import CoordinationStore, StoreBusy
from agent_comms.coordination_errors import IntegrityViolationError, SchemaVersionError
from agent_comms.coordination_schema import (
    COORDINATION_SCHEMA_VERSION,
    COORDINATION_SNAPSHOT_VERSION,
)
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import (
    ExecutionAssignmentLink,
    MessageAudience,
    WakeAssignment,
)
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayAssessments, ReplayFact
from agent_comms.coordination_tables.executions import (
    CurrentExecutions,
    ExecutionOrigin,
    ExecutionRecord,
)
from agent_comms.coordination_tables.participants import OwnerGenerations, Participants
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    PublicationReceipts,
    canonical_publication_key,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.execution_states import (
    ActiveExecution,
    CompletedExecution,
    DeferredExecution,
    ExecutionState,
    FailedExecution,
    PendingExecution,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.typed_table import JsonStorage
from agent_comms.messages import InfoKind, Message
from agent_comms.obligation_states import (
    PendingResponse,
    PublishedResponse,
    SilentResponse,
)
from agent_comms.wake_policy import FullWake


def execution(*, status=PendingExecution, ordinal=None, origin=ExecutionOrigin.ACP):
    return ExecutionRecord(
        execution_id="execution-1",
        origin=origin,
        owner_thread="worker",
        owner_lookup="owner-1",
        revision=1,
        max_attempts=3,
        reason_code=None,
        created_at_ms=100,
        updated_at_ms=100,
        lifecycle=status.load(ordinal),
    )


def attempt(*, ordinal=1, phase=PromptStartingAttempt, generation=1, done=False, dead=False):
    return AttemptRecord(
        execution_id="execution-1",
        attempt_ordinal=ordinal,
        owner_lookup="owner-1",
        owner_thread="worker",
        owner_generation=generation,
        owner_token_digest=f"digest-{generation}",
        revision=1,
        last_progress_at_ms=None,
        reason_code=None,
        created_at_ms=100,
        updated_at_ms=100,
        lifecycle=phase.load(None if phase.terminal else 500, done, dead),
    )


def assignment():
    return WakeAssignment(
        assignment_id="claim-1",
        recipient="worker",
        recipient_lookup="owner-1",
        wire_seq=2,
        message_id="message-1",
        audience=MessageAudience.DIRECT,
        accepted_at_ms=100,
        updated_at_ms=100,
        revision=1,
        lifecycle=EngagedAssignment.load(FullWake(), None, "execution-1", "requester"),
    )


def obligation(state=PendingResponse):
    return ResponseObligation(
        execution_id="execution-1",
        exact_target="requester",
        reason_code=None,
        created_at_ms=100,
        updated_at_ms=100,
        revision=1,
        lifecycle=state.load(None, None),
    )


def snapshot(
    *,
    execution_record=None,
    attempt_record=None,
    origin=ExecutionOrigin.ACP,
    current=False,
    response=None,
    replay=None,
):
    record = execution_record or execution(origin=origin)
    return RecoverySnapshot(
        execution=record,
        attempt=attempt_record,
        links=(
            (ExecutionAssignmentLink(record.execution_id, "claim-1", 0),)
            if record.origin is ExecutionOrigin.WIRE
            else ()
        ),
        assignments=(
            (
                (
                    replace(
                        assignment(),
                        lifecycle=record.lifecycle.assignment_state().build(
                            assignment().lifecycle.mode,
                            record.execution_id,
                            assignment().lifecycle.exact_target,
                        ),
                    )
                ),
            )
            if record.origin is ExecutionOrigin.WIRE
            else ()
        ),
        replay=replay or ReplayAssessments(record.execution_id, ReplayFact.NONE, True, False, 1),
        obligations=(response,) if record.origin is ExecutionOrigin.WIRE and response else (),
        publication_intents=(),
        publication_receipts=(),
        connectivity=None,
        last_recovery=None,
        current_execution_id=record.execution_id if current else None,
        current_attempt_ordinal=record.lifecycle.current_attempt_ordinal if current else None,
        pointer_revision=1,
        is_current=current,
    )


def sql_execution(
    db,
    name="e",
    status="pending",
    origin="acp",
    ordinal=None,
    max_attempts=3,
    owner="p",
    thread="worker",
):
    ExecutionRecord(
        execution_id=name, origin=ExecutionOrigin(origin),
        lifecycle=ExecutionState.decode(status).load(ordinal),
        owner_thread=thread, owner_lookup=owner, revision=1,
        max_attempts=max_attempts, reason_code=None, created_at_ms=0, updated_at_ms=0,
    ).insert(db)


def sql_wire_execution(db, *, pending=True, name="e", target="requester"):
    db.execute("BEGIN IMMEDIATE")
    sql_execution(db, name=name, status="queued", origin="wire")
    WakeAssignment(
        assignment_id="a", recipient="worker", recipient_lookup="p",
        wire_seq=2, message_id="m", audience=MessageAudience.DIRECT,
        lifecycle=EngagedAssignment.build(FullWake(), name, target),
        accepted_at_ms=0, updated_at_ms=0, revision=1,
    ).insert(db)
    ExecutionAssignmentLink(name, "a", 0).insert(db)
    ResponseObligation(name, target, PendingResponse(), None, 0, 0, 1).insert(db)
    if pending:
        db.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','pending'),revis"
                "ion=2 WHERE execution_id=?"
            ),
            (name,),
        )
    db.execute("COMMIT")


def sql_attempt(db, name="e", ordinal=1, generation=1, owner="p", thread="worker", digest=None):
    AttemptRecord(
        execution_id=name, attempt_ordinal=ordinal, owner_lookup=owner,
        owner_thread=thread, owner_generation=generation,
        owner_token_digest=digest if digest is not None else f"digest-{generation}",
        lifecycle=PromptStartingAttempt.load(500, False, False), revision=1,
        last_progress_at_ms=None, reason_code=None, created_at_ms=0, updated_at_ms=0,
    ).insert(db)


def owner(db):
    Participants(participant_lookup="p", display_name="worker", committed=True).insert(db)
    OwnerGenerations(owner_lookup="p", owner_thread="worker", generation=1).insert(db)
    CurrentExecutions(owner_lookup="p", execution_id=None, attempt_ordinal=None, pointer_revision=0).insert(db)


def activate(db, name="e", ordinal=1, generation=1):
    db.execute("BEGIN IMMEDIATE")
    sql_attempt(db, name, ordinal, generation)
    db.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
            "nal',?),revision=revision+1 WHERE execution_id=?"
        ),
        (ordinal, name),
    )
    db.execute(
        "UPDATE current_executions SET execution_id=?,attempt_ordinal=?,"
        "pointer_revision=pointer_revision+1 WHERE owner_lookup='p'",
        (name, ordinal),
    )
    db.execute("COMMIT")


def fail_defer(db, name="e", ordinal=1):
    if (
        db.execute("SELECT 1 FROM replay_assessments WHERE execution_id=?", (name,)).fetchone()
        is None
    ):
        db.execute("INSERT INTO replay_assessments VALUES (?,0,1,0,1)", (name,))
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        (
            "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision="
            "revision+1 WHERE execution_id=? AND attempt_ordinal=?"
        ),
        (name, ordinal),
    )
    db.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','deferred'),revi"
            "sion=revision+1 WHERE execution_id=?"
        ),
        (name,),
    )
    db.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=pointer_revision+1 WHERE owner_lookup='p'"
    )
    db.execute("COMMIT")


def test_total_enum_partition_and_transitions():
    assert set(ExecutionState.names()) == {
        "queued",
        "pending",
        "active",
        "deferred",
        "completed",
        "failed",
    }
    assert RetryingAttempt in AbortingAttempt.successors()
    assert AttemptFailedAttempt.terminal
    assert ActiveExecution not in {CompletedExecution, FailedExecution}


def test_typed_execution_and_attempt_authority_are_separate():
    pending = execution()
    first = attempt()
    with pytest.raises(IntegrityViolationError):
        replace(
            first, lifecycle=AttemptFailedAttempt.load(None, True, first.lifecycle.process_dead)
        )
    with pytest.raises(FrozenInstanceError):
        first.attempt_ordinal = 2
    with pytest.raises(FrozenInstanceError):
        pending.lifecycle = FailedExecution()




def test_snapshot_projection_retry_and_inert_secrets():
    pre_failed = snapshot(execution_record=execution(status=FailedExecution))
    assert pre_failed.can_retry is False
    failed_attempt = attempt(phase=AttemptFailedAttempt, done=True, dead=True)
    deferred = snapshot(
        execution_record=execution(status=DeferredExecution, ordinal=1),
        attempt_record=failed_attempt,
    )
    assert deferred.can_retry
    projection = FieldCodec.project(deferred, "snapshot")
    assert projection["execution"]["current_attempt_ordinal"] == 1
    assert projection["attempt"]["owner_generation"] == 1
    assert "owner_token_digest" not in str(projection)
    assert "digest-1" not in str(projection)
    with pytest.raises(IntegrityViolationError, match="budget"):
        replace(deferred.execution, max_attempts=1)
    unsafe = ReplayAssessments("execution-1", ReplayFact.TOOL_EXECUTED, False, True, 2)
    with pytest.raises(IntegrityViolationError, match="authorized retry"):
        snapshot(execution_record=deferred.execution, attempt_record=failed_attempt, replay=unsafe)
    with pytest.raises(IntegrityViolationError, match="authorized retry"):
        snapshot(
            execution_record=replace(
                deferred.execution,
                lifecycle=FailedExecution.load(
                    deferred.execution.lifecycle.current_attempt_ordinal
                ),
            ),
            attempt_record=failed_attempt,
        )
    failed_unsafe = snapshot(
        execution_record=replace(
            deferred.execution,
            lifecycle=FailedExecution.load(deferred.execution.lifecycle.current_attempt_ordinal),
        ),
        attempt_record=failed_attempt,
        replay=unsafe,
    )
    assert not failed_unsafe.can_retry
    active = snapshot(
        execution_record=execution(status=ActiveExecution, ordinal=1),
        attempt_record=attempt(),
        current=True,
    )
    assert active.is_current and not active.can_retry
    with pytest.raises(IntegrityViolationError):
        replace(active, current_attempt_ordinal=2)
    with pytest.raises(IntegrityViolationError):
        replace(active, is_current=False, current_execution_id=None, current_attempt_ordinal=None)


def test_wire_claim_obligation_and_publication_contract():
    record = execution(origin=ExecutionOrigin.WIRE)
    valid = snapshot(execution_record=record, response=obligation())
    assert FieldCodec.project(valid, "snapshot")["claims"][0]["claim_id"] == "claim-1"
    primitive = FieldCodec.project(valid, "snapshot")
    assert primitive["execution_claims"][0]["ordinal"] == 0
    assert "claim_ids" not in primitive["execution"]
    assert [item["claim_id"] for item in primitive["claims"]] == [
        item["claim_id"] for item in primitive["execution_claims"]
    ]
    with pytest.raises(IntegrityViolationError, match="ordered claims"):
        replace(valid, links=(ExecutionAssignmentLink(record.execution_id, "claim-1", 1),))
    with pytest.raises(IntegrityViolationError, match="ordered claims"):
        replace(valid, links=(ExecutionAssignmentLink("other", "claim-1", 0),))
    with pytest.raises(IntegrityViolationError):
        replace(valid, assignments=(replace(assignment(), recipient_lookup="other"),))
    with pytest.raises(IntegrityViolationError, match="disposition"):
        replace(
            valid,
            assignments=(
                replace(
                    assignment(),
                    lifecycle=CompletedAssignment.load(
                        assignment().lifecycle.mode,
                        assignment().lifecycle.verdict,
                        assignment().lifecycle.execution_id,
                        assignment().lifecycle.exact_target,
                    ),
                ),
            ),
        )
    with pytest.raises(IntegrityViolationError, match="disposition"):
        replace(
            valid,
            execution=replace(
                record, lifecycle=FailedExecution.load(record.lifecycle.current_attempt_ordinal)
            ),
        )
    with pytest.raises(IntegrityViolationError, match="authorized retry"):
        snapshot(
            execution_record=replace(record, lifecycle=DeferredExecution.load(1)),
            attempt_record=attempt(phase=AttemptFailedAttempt, done=True, dead=True),
            response=obligation(SilentResponse),
        )
    with pytest.raises(IntegrityViolationError):
        snapshot(
            execution_record=replace(record, lifecycle=CompletedExecution.load(1)),
            attempt_record=attempt(phase=SucceededAttempt, done=True, dead=True),
            response=obligation(),
        )
    assert canonical_publication_key("execution-1", "#route:variant") == (
        "publication:v1:execution-1:#route:variant"
    )
    with pytest.raises(ValueError):
        canonical_publication_key("bad:id", "requester")
    body = "Reply"
    message = Message(
        sender="worker",
        target="requester",
        body=body,
        type=InfoKind,
        timestamp=1.0,
        notice=False,
    )
    intent = PublicationIntents(
        execution_id="execution-1",
        exact_target="requester",
        message=message,
        publication_key=canonical_publication_key("execution-1", "requester"),
        expected_message_id=message.message_id,
    )
    with pytest.raises(IntegrityViolationError):
        replace(intent, expected_message_id="fabricated")




@pytest.fixture
def db(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with CoordinationStore(path) as store:
        owner(store._connection)
        store._connection.row_factory = None
        yield store._connection, path


def test_database_version_privacy_and_reopen(db):
    connection, path = db
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA user_version").fetchone()[0] == COORDINATION_SCHEMA_VERSION
    assert COORDINATION_SNAPSHOT_VERSION == 3
    if os.name != "nt":  # NTFS access is governed by ACLs, not POSIX mode bits.
        assert os.stat(path).st_mode & 0o777 == 0o600
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened.schema_version == COORDINATION_SCHEMA_VERSION
        assert (
            reopened._connection.execute("SELECT participant_lookup FROM participants").fetchone()[
                0
            ]
            == "p"
        )


def test_current_schema_open_is_read_only_while_readers_and_writer_are_live(db):
    """Opening a coordinator must not demand a write COMMIT before wire exclusion."""
    _, path = db
    with (
        closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as reader,
        closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as writer,
    ):
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM schema_meta").fetchall()
        writer.execute("BEGIN IMMEDIATE")
        try:
            with CoordinationStore(path, StoreBusy()) as reopened:
                assert reopened.schema_version == COORDINATION_SCHEMA_VERSION
                assert not reopened._connection.in_transaction
                assert reopened._connection.total_changes == 0
        finally:
            reader.execute("ROLLBACK")
            writer.execute("ROLLBACK")


def test_concurrent_fresh_initializers_serialize_and_reopen(tmp_path):
    script = """import sys
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_schema import COORDINATION_SCHEMA_VERSION
sys.stdin.buffer.read(1)
with CoordinationStore(sys.argv[1]) as store:
    assert store.schema_version == COORDINATION_SCHEMA_VERSION
    assert store._connection.execute("SELECT count(*) FROM schema_meta").fetchone()[0] == 1
print("ready")
"""
    for wave in range(8):
        path = tmp_path / f"concurrent-{wave}.sqlite3"
        children = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(12)
        ]
        try:
            for child in children:
                assert child.stdin is not None
                child.stdin.write(b"x")
                child.stdin.flush()
                child.stdin.close()
                child.stdin = None
            for child in children:
                output, error = child.communicate(timeout=45)
                assert child.returncode == 0, (wave, error.decode(errors="replace"))
                assert output.strip() == b"ready"
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        with CoordinationStore(path) as reopened:
            assert reopened.schema_version == COORDINATION_SCHEMA_VERSION
            assert (
                reopened._connection.execute("SELECT count(*) FROM schema_meta").fetchone()[0] == 1
            )
            assert reopened._connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_first_create_never_closes_a_published_target_fd(tmp_path, monkeypatch):
    path = tmp_path / "atomic-create.sqlite3"
    before_link = threading.Event()
    publish = threading.Event()
    original_link = os.link
    errors = []

    def paused_link(source, destination, *, follow_symlinks=True):
        if destination == path:
            before_link.set()
            assert publish.wait(15)
        return original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", paused_link)

    def creator():
        try:
            with CoordinationStore(path) as store:
                assert store.schema_version == COORDINATION_SCHEMA_VERSION
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=creator)
    thread.start()
    try:
        assert before_link.wait(15)
        assert not path.exists()
        with pytest.raises(sqlite3.OperationalError):
            sqlite3.connect(f"file:{path}?mode=rw", uri=True).execute("BEGIN IMMEDIATE")
    finally:
        publish.set()
        thread.join(timeout=20)
    assert not thread.is_alive() and not errors

    probe = """import json
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=rw", uri=True, timeout=0.15)
try:
    c.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as exc:
    assert "locked" in str(exc)
    print("blocked")
else:
    print("acquired")
    c.rollback()
c.close()
"""
    with CoordinationStore(path) as store:
        c = store._connection
        c.execute("BEGIN IMMEDIATE")
        owner(c)
        child = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert child.stdout.strip() == b"blocked", child.stderr
        c.execute("COMMIT")
        child = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert child.stdout.strip() == b"acquired", child.stderr
    with CoordinationStore(path) as reopened:
        assert reopened._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 1


def test_activating_requires_transactional_exact_pointer_and_fence(db):
    connection, path = db
    sql_execution(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
                "nal',1),revision=2 WHERE execution_id='e'"
            )
        )
    with pytest.raises(sqlite3.IntegrityError):
        sql_attempt(connection, generation=2)
    activate(connection)
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        c = reopened._connection
        assert c.execute("SELECT status,current_attempt_ordinal FROM executions").fetchone() == (
            "active",
            1,
        )
        assert c.execute(
            "SELECT execution_id,attempt_ordinal FROM current_executions"
        ).fetchone() == ("e", 1)
        assert c.execute("SELECT phase,backend_done,process_dead FROM attempts").fetchone() == (
            "prompt_starting",
            0,
            0,
        )


def test_attempt_only_autocommit_or_transaction_commit_cannot_claim_activity(db):
    c, path = db
    sql_execution(c)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        sql_attempt(c)
    assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    c.execute("BEGIN IMMEDIATE")
    sql_attempt(c)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")
    c.execute("ROLLBACK")
    with CoordinationStore(path) as reopened:
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_invalid_multistatement_commit_rolls_back_and_reopens(db):
    connection, path = db
    sql_execution(connection)
    connection.execute("BEGIN IMMEDIATE")
    sql_attempt(connection)
    connection.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
            "nal',1),revision=2 WHERE execution_id='e'"
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute("COMMIT")
    connection.execute("ROLLBACK")
    assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    assert connection.execute("SELECT status,revision FROM executions").fetchone() == ("pending", 1)
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_attempt_terminal_commit_requires_matching_execution_and_no_pointer(db):
    c, path = db
    sql_execution(c)
    activate(c)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            (
                "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision="
                "2 WHERE execution_id='e'"
            )
        )
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        (
            "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision="
            "2 WHERE execution_id='e'"
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")
    c.execute("ROLLBACK")
    c.execute("INSERT INTO replay_assessments VALUES ('e',0,1,0,1)")
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        (
            "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision="
            "2 WHERE execution_id='e'"
        )
    )
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','deferred'),revi"
            "sion=3 WHERE execution_id='e'"
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")
    c.execute("ROLLBACK")
    fail_defer(c)
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        rows = reopened._connection.execute(
            "SELECT status,current_attempt_ordinal FROM executions"
        ).fetchone()
        assert rows == ("deferred", 1)
        assert reopened._connection.execute(
            "SELECT phase,backend_done,process_dead FROM attempts"
        ).fetchone() == ("attempt_failed", 1, 1)


def test_retry_creates_contiguous_fresh_attempt_and_preserves_n(db):
    c, path = db
    sql_execution(c)
    activate(c)
    fail_defer(c)
    with pytest.raises(sqlite3.IntegrityError):
        sql_attempt(c, ordinal=3, generation=1)
    c.execute("UPDATE owner_generations SET generation=2 WHERE owner_lookup='p'")
    c.execute("BEGIN IMMEDIATE")
    sql_attempt(c, ordinal=2, generation=2)
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
            "nal',2),revision=4 WHERE execution_id='e'"
        )
    )
    c.execute(
        "UPDATE current_executions SET execution_id='e',attempt_ordinal=2,"
        "pointer_revision=3 WHERE owner_lookup='p'"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        rows = reopened._connection.execute(
            "SELECT attempt_ordinal,owner_generation,backend_done,process_dead,phase "
            "FROM attempts ORDER BY attempt_ordinal"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, 1, 1, 1, "attempt_failed"),
            (2, 2, 0, 0, "prompt_starting"),
        ]
        assert reopened._connection.execute(
            "SELECT status,current_attempt_ordinal FROM executions"
        ).fetchone() == ("active", 2)
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        (
            "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.kind','prompt_accepted')"
            ",revision=2 WHERE execution_id='e' AND attempt_ordinal=2"
        )
    )
    c.execute(
        (
            "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.kind','model_running'),r"
            "evision=3 WHERE execution_id='e' AND attempt_ordinal=2"
        )
    )
    c.execute(
        (
            "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.kind','settling'),revisi"
            "on=4 WHERE execution_id='e' AND attempt_ordinal=2"
        )
    )
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','completed'),rev"
            "ision=5 WHERE execution_id='e'"
        )
    )
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=4 WHERE owner_lookup='p'"
    )
    c.execute(
        (
            "UPDATE attempts SET lifecycle=json_object('kind','succeeded'),revision=5 WHE"
            "RE execution_id='e' AND attempt_ordinal=2"
        )
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT status,current_attempt_ordinal FROM executions"
        ).fetchone() == ("completed", 2)
        assert reopened._connection.execute(
            "SELECT attempt_ordinal,phase FROM attempts ORDER BY attempt_ordinal"
        ).fetchall() == [(1, "attempt_failed"), (2, "succeeded")]


def test_deferred_replay_authority_cannot_be_revoked_while_deferred(db):
    c, path = db
    sql_execution(c)
    activate(c)
    fail_defer(c)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("UPDATE replay_assessments SET replay_safe=0,revision=2 WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute(
            "UPDATE replay_assessments SET side_effects_possible=1,revision=2 "
            "WHERE execution_id='e'"
        )
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT replay_safe FROM replay_assessments").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("authority, refusal", [
    ("missing", "FOREIGN KEY"),
    ("unsafe", "FOREIGN KEY"),
    ("budget", "CHECK constraint failed"),
    ("published", "deferred execution requires all original responses retryable"),
])
def test_failed_attempt_must_settle_failed_when_retry_unauthorized(db, authority, refusal):
    c, path = db
    if authority == "published":
        sql_wire_execution(c)
        c.execute(
            (
                "UPDATE obligations SET lifecycle=json_object('kind','silent'),revision=2 WHE"
                "RE execution_id='e'"
            )
        )
    else:
        sql_execution(c, max_attempts=1 if authority == "budget" else 3)
    activate(c)
    if authority == "unsafe":
        c.execute("INSERT INTO replay_assessments VALUES ('e',8,0,1,1)")
    if authority in {"budget", "published"}:
        c.execute("INSERT INTO replay_assessments VALUES ('e',0,1,0,1)")
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision=2 "
        "WHERE execution_id='e'"
    )
    with pytest.raises(sqlite3.IntegrityError, match=refusal):
        c.execute(
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','deferred'),"
            "revision=revision+1 WHERE execution_id='e'"
        )
        if authority == "published":
            c.execute(
                "UPDATE wake_claims SET lifecycle=json_set(lifecycle,'$.kind','deferred'),"
                "revision=2 WHERE assignment_id='a'"
            )
        c.execute(
            "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
            "pointer_revision=2 WHERE owner_lookup='p'"
        )
        c.execute("COMMIT")
    c.execute("ROLLBACK")
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision=2 "
        "WHERE execution_id='e'"
    )
    c.execute(
        "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'),"
        "revision=revision+1 WHERE execution_id='e'"
    )
    if authority == "published":
        c.execute(
            "UPDATE wake_claims SET lifecycle=json_set(lifecycle,'$.kind','failed'),"
            "revision=2 WHERE assignment_id='a'"
        )
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert (
            reopened._connection.execute("SELECT status FROM executions").fetchone()[0] == "failed"
        )
        assert reopened._connection.execute(
            "SELECT phase,backend_done,process_dead FROM attempts"
        ).fetchone() == ("attempt_failed", 1, 1)


def test_authorized_retry_rejects_post_attempt_failed_and_cannot_arrive_later(db):
    c, path = db
    sql_execution(c)
    activate(c)
    c.execute("INSERT INTO replay_assessments VALUES ('e',0,1,0,1)")
    with pytest.raises(sqlite3.IntegrityError, match="authorized retry"):
        c.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'),revisi"
                "on=3 WHERE execution_id='e'"
            )
        )
    c.execute("UPDATE replay_assessments SET replay_safe=0,revision=2 WHERE execution_id='e'")
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision=2 "
        "WHERE execution_id='e'"
    )
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'),revisi"
            "on=3 WHERE execution_id='e'"
        )
    )
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute("COMMIT")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE replay_assessments SET replay_safe=1,revision=3 WHERE execution_id='e'")
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT status FROM executions").fetchone()[0] == "failed"
        )


def test_missing_replay_cannot_later_reauthorize_failed(db):
    c, path = db
    sql_execution(c)
    activate(c)
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "UPDATE attempts SET lifecycle=json_object('kind','attempt_failed'),revision=2 "
        "WHERE execution_id='e'"
    )
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'),revisi"
            "on=3 WHERE execution_id='e'"
        )
    )
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM replay_assessments").fetchone()[0]
            == 0
        )
        execution_record = ExecutionRecord.one(reopened._connection, execution_id="e")
        attempt_record = AttemptRecord.one(reopened._connection, execution_id="e", attempt_ordinal=1)
        assert execution_record is not None and attempt_record is not None
        absent = RecoverySnapshot(
            execution=execution_record,
            attempt=attempt_record,
            assignments=(),
            links=(),
            replay=None,
            obligations=(),
            publication_intents=(),
            publication_receipts=(),
            connectivity=None,
            last_recovery=None,
            current_execution_id=None,
            current_attempt_ordinal=None,
            pointer_revision=2,
            is_current=False,
        )
        assert FieldCodec.project(absent, "snapshot")["replay"] is None
        assert not absent.can_retry
        with pytest.raises(IntegrityViolationError, match="authorized retry"):
            replace(
                absent,
                execution=replace(
                    execution_record,
                    lifecycle=DeferredExecution.load(
                        execution_record.lifecycle.current_attempt_ordinal
                    ),
                ),
            )
    with pytest.raises(sqlite3.IntegrityError, match="authorized retry"):
        c.execute("INSERT INTO replay_assessments VALUES ('e',0,1,0,1)")
    c.execute("INSERT INTO replay_assessments VALUES ('e',8,0,1,1)")
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT replay_safe FROM replay_assessments").fetchone()[0]
            == 0
        )


def test_owner_generation_cannot_advance_while_attempt_active(db):
    c, _path = db
    sql_execution(c)
    activate(c)
    with pytest.raises(sqlite3.IntegrityError, match="active attempts"):
        c.execute("UPDATE owner_generations SET generation=2 WHERE owner_lookup='p'")
    with pytest.raises(sqlite3.IntegrityError):
        sql_attempt(c, ordinal=2, generation=1)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM owner_generations")


def test_direct_sql_immutable_attempt_identity_and_monotonic_evidence(db):
    c, path = db
    sql_execution(c)
    activate(c)
    for clause in (
        "attempt_ordinal=2",
        "owner_generation=2",
        "owner_token_digest='changed'",
        "owner_thread='renamed'",
        "lifecycle=json_set(lifecycle,'$.backend_done',json('true'),'$.process_dead',json('true'),"
        "'$.lease_expires_at_ms',499),revision=2,last_progress_at_ms=5,updated_at_ms=5",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"UPDATE attempts SET {clause} WHERE execution_id='e'")
    c.execute(
        (
            "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.backend_done',json('true"
            "')),revision=2 WHERE execution_id='e'"
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            (
                "UPDATE attempts SET lifecycle=json_set(lifecycle,'$.backend_done',json('fals"
                "e')),revision=3 WHERE execution_id='e'"
            )
        )
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT backend_done,revision FROM attempts"
        ).fetchone() == (1, 2)


def test_wire_execution_requires_claim_and_obligation_at_commit(db):
    c, path = db
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        sql_execution(c, origin="wire")
    assert c.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    sql_wire_execution(c)
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM execution_claims").fetchone()[0] == 1
        )
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0] == "pending"
        )


@pytest.mark.parametrize("settle_attempt, settle_response", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_completed_wire_requires_succeeded_attempt_and_terminal_obligation(
    db, settle_attempt, settle_response,
):
    c, path = db
    sql_wire_execution(c)
    activate(c)
    c.execute("BEGIN IMMEDIATE")
    for revision, phase in enumerate(
        (PromptAcceptedAttempt, ModelRunningAttempt, SettlingAttempt), 2,
    ):
        AttemptRecord.update(c, where="execution_id='e'", revision=revision,
                             lifecycle=phase.load(500, False, False))
    if settle_attempt:
        AttemptRecord.update(c, where="execution_id='e'", revision=5,
                             lifecycle=SucceededAttempt())
    if settle_response:
        ResponseObligation.update(c, where="execution_id='e'", revision=2,
                                  lifecycle=SilentResponse())
    WakeAssignment.update(c, where="assignment_id='a'", revision=2,
                          lifecycle=CompletedAssignment.build(FullWake(), "e", "requester"))
    CurrentExecutions.update(c, where="owner_lookup='p'", execution_id=None,
                             attempt_ordinal=None, pointer_revision=2)
    completion = (
        "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','completed'),"
        "revision=revision+1 WHERE execution_id='e'"
    )
    if not settle_response:
        with pytest.raises(sqlite3.IntegrityError, match="all original responses"):
            c.execute(completion)
        c.execute("ROLLBACK")
    elif not settle_attempt:
        c.execute(completion)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            c.execute("COMMIT")
        c.execute("ROLLBACK")
    else:
        c.execute(completion)
        c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute("SELECT status FROM executions").fetchone()[0] == (
            "completed" if settle_attempt and settle_response else "active"
        )
        assert reopened._connection.execute("SELECT phase FROM attempts").fetchone()[0] == (
            "succeeded" if settle_attempt and settle_response else "prompt_starting"
        )
        assert reopened._connection.execute("SELECT state FROM obligations").fetchone()[0] == (
            "silent" if settle_attempt and settle_response else "pending"
        )


def test_claim_execution_status_partition_is_atomic_at_commit(db):
    c, path = db
    sql_wire_execution(c, pending=False)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute((
                  "UPDATE wake_claims SET lifecycle=json_set(lifecycle,'$.kind','failed'),revis"
                  "ion=2 WHERE assignment_id='a'"
                  ))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'),revisi"
                "on=2 WHERE execution_id='e'"
            )
        )
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'),revisi"
            "on=2 WHERE execution_id='e'"
        )
    )
    c.execute((
              "UPDATE wake_claims SET lifecycle=json_set(lifecycle,'$.kind','failed'),revis"
              "ion=2 WHERE assignment_id='a'"
              ))
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert (
            reopened._connection.execute("SELECT status FROM executions").fetchone()[0] == "failed"
        )
        assert (
            reopened._connection.execute("SELECT disposition FROM wake_claims").fetchone()[0]
            == "failed"
        )


def test_membership_ordinals_contiguous_and_frozen_after_activation(db):
    c, path = db
    sql_wire_execution(c, pending=False)

    original = WakeAssignment.one(c, assignment_id="a")
    assert original is not None

    def bind(identifier, seq):
        replace(original, assignment_id=identifier, wire_seq=seq).insert(c)

    c.execute("BEGIN IMMEDIATE")
    bind("b", 3)
    with pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        c.execute("INSERT INTO execution_claims VALUES ('e','b',2)")
    c.execute("INSERT INTO execution_claims VALUES ('e','b',1)")
    c.execute("COMMIT")
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','pending'),revis"
            "ion=2 WHERE execution_id='e'"
        )
    )
    c.execute("BEGIN IMMEDIATE")
    bind("c", 4)
    c.execute("INSERT INTO execution_claims VALUES ('e','c',2)")
    c.execute("COMMIT")
    activate(c)
    c.execute("BEGIN IMMEDIATE")
    bind("d", 5)
    with pytest.raises(sqlite3.IntegrityError, match="membership"):
        c.execute("INSERT INTO execution_claims VALUES ('e','d',3)")
    c.execute("ROLLBACK")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        bind("orphan", 6)
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT assignment_id,ordinal FROM execution_claims ORDER BY ordinal"
        ).fetchall() == [("a", 0), ("b", 1), ("c", 2)]


def test_new_attempt_fence_must_advance_and_change_digest(db):
    c, path = db
    sql_execution(c)
    activate(c)
    fail_defer(c)
    with pytest.raises(sqlite3.IntegrityError, match="older distinct fence"):
        sql_attempt(c, ordinal=2, generation=1)
    c.execute("UPDATE owner_generations SET generation=2 WHERE owner_lookup='p'")
    with pytest.raises(sqlite3.IntegrityError, match="older distinct fence"):
        sql_attempt(c, ordinal=2, generation=2, digest="digest-1")
    c.execute("UPDATE owner_generations SET generation=3 WHERE owner_lookup='p'")
    c.execute("BEGIN IMMEDIATE")
    sql_attempt(c, ordinal=2, generation=3)
    c.execute(
        (
            "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','active','$.ordi"
            "nal',2),revision=4 WHERE execution_id='e'"
        )
    )
    c.execute(
        "UPDATE current_executions SET execution_id='e',attempt_ordinal=2,"
        "pointer_revision=3 WHERE owner_lookup='p'"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT owner_generation,owner_token_digest FROM attempts ORDER BY attempt_ordinal"
        ).fetchall() == [(1, "digest-1"), (3, "digest-3")]


def test_fence_digest_is_globally_unique_across_executions(db):
    c, path = db
    sql_execution(c)
    activate(c)
    Participants(participant_lookup="q", display_name="other-worker", committed=True).insert(c)
    OwnerGenerations(owner_lookup="q", owner_thread="other-worker", generation=1).insert(c)
    CurrentExecutions(owner_lookup="q", execution_id=None, attempt_ordinal=None, pointer_revision=0).insert(c)
    sql_execution(c, name="other-e", owner="q", thread="other-worker")
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        sql_attempt(c, name="other-e", owner="q", thread="other-worker", digest="digest-1")
    c.execute("ROLLBACK")
    with CoordinationStore(path) as reopened:
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1




def test_claim_target_and_recipient_lineage_is_immutable(db):
    c, path = db
    sql_wire_execution(c, pending=False)
    Participants(participant_lookup="other", display_name="other-worker", committed=True).insert(c)
    original = WakeAssignment.one(c, assignment_id="a")
    assert original is not None
    c.execute("BEGIN IMMEDIATE")
    replace(original, assignment_id="b", recipient="other-worker",
            recipient_lookup="other", wire_seq=3).insert(c)
    with pytest.raises(sqlite3.IntegrityError, match="claim target"):
        c.execute("INSERT INTO execution_claims VALUES ('e','b',1)")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")  # Orphan execution-bound claim is not durable.
    c.execute("ROLLBACK")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE execution_claims SET ordinal=1 WHERE assignment_id='a'")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert (
            reopened._connection.execute("SELECT assignment_id FROM execution_claims").fetchone()[0]
            == "a"
        )
        assert reopened._connection.execute("SELECT count(*) FROM wake_claims").fetchone()[0] == 1


def test_populated_durable_authorities_reject_delete_and_survive_reopen(db):
    c, path = db
    sql_wire_execution(c)
    activate(c)
    c.execute("INSERT INTO participant_aliases VALUES ('old-worker','p',100)")
    c.execute("INSERT INTO replay_assessments VALUES ('e',0,1,0,1)")
    c.execute("INSERT INTO connectivity VALUES ('e','connected','connected',1,100)")
    c.execute(
        "INSERT INTO recovery_audit (execution_id,kind,reason_code,"
        "sanitized_detail,attempt,elapsed_ms,observed_at_ms) "
        "VALUES ('e','model_stalled','timeout',NULL,1,100,100)"
    )
    tables = (
        "schema_meta",
        "participants",
        "participant_aliases",
        "owner_generations",
        "executions",
        "attempts",
        "current_executions",
        "wake_claims",
        "execution_claims",
        "replay_assessments",
        "obligations",
        "connectivity",
        "recovery_audit",
    )
    for table in tables:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"DELETE FROM {table}")
    with CoordinationStore(path) as reopened:
        for table in tables:
            assert reopened._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


def sql_intent_envelope(*, body="hello", target="requester", expected_id=None, message_json=None):
    message = Message(
        sender="worker",
        target="requester",
        body=body,
        type=InfoKind,
        timestamp=1.0,
        notice=False,
    )
    return (
        "e",
        target,
        message_json if message_json is not None else JsonStorage.to_sql(message),
        canonical_publication_key("e", "requester"),
        expected_id if expected_id is not None else message.message_id,
    )


INSERT_INTENT = "INSERT INTO publication_intents VALUES (?,?,?,?,?)"


@pytest.mark.parametrize(
    "mutation",
    ["invented_message_id", "byte_cap", "infinite_timestamp", "route_not_message_target", "not_json"],
)
def test_sql_publication_envelope_rejects_invalid_commits_and_reopens(db, mutation):
    c, path = db
    sql_wire_execution(c)
    envelope = {
        "invented_message_id": lambda: sql_intent_envelope(expected_id="not-message-id"),
        "byte_cap": lambda: sql_intent_envelope(body="界" * 40001),
        "infinite_timestamp": lambda: sql_intent_envelope(
            message_json=sql_intent_envelope()[2].replace('"ts":1.0', '"ts":1e999')
        ),
        "route_not_message_target": lambda: sql_intent_envelope(target="other"),
        "not_json": lambda: sql_intent_envelope(message_json="{"),
    }[mutation]()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(INSERT_INTENT, envelope)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(INSERT_INTENT, envelope)
    c.execute("ROLLBACK")
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_intents").fetchone()[0]
            == 0
        )
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0] == "pending"
        )


@pytest.mark.parametrize(
    "position,value",
    [(0, 123), (1, 123), (1, True), (3, 123), (4, 123)],
)
def test_publication_text_columns_reject_numeric_affinity(db, position, value):
    c, path = db
    sql_wire_execution(c)
    envelope = list(sql_intent_envelope())
    envelope[position] = value
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(INSERT_INTENT, envelope)
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_intents").fetchone()[0]
            == 0
        )


def test_numeric_only_message_id_input_is_not_text(db):
    c, path = db
    sql_wire_execution(c)
    for number in range(10000):
        body = f"numeric-id-{number}"
        message = Message(
            sender="worker",
            target="requester",
            body=body,
            type=InfoKind,
            timestamp=1.0,
            notice=False,
        )
        if message.message_id.isdigit() and not message.message_id.startswith("0"):
            break
    else:
        raise AssertionError("no numeric-only Message ID in bounded search")
    envelope = sql_intent_envelope(body=body, expected_id=int(message.message_id))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(INSERT_INTENT, envelope)
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_intents").fetchone()[0]
            == 0
        )
    c.execute("BEGIN IMMEDIATE")
    c.execute(INSERT_INTENT, (*envelope[:-1], message.message_id))
    c.execute(
        (
            "UPDATE obligations SET lifecycle=json_object('kind','publishing'),revision=2"
            " WHERE execution_id='e'"
        )
    )
    c.execute("COMMIT")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO publication_receipts VALUES ('e','requester',1,?,100)",
            (int(message.message_id),),
        )
    c.execute("BEGIN IMMEDIATE")
    PublicationReceipts(execution_id="e", exact_target="requester", seq=1,
                        message_id=message.message_id, received_at_ms=100).insert(c)
    c.execute(
        (
            "UPDATE obligations SET lifecycle=json_object('kind','published','message_id'"
            ",?,'seq',1),revision=3 WHERE execution_id='e'"
        ),
        (message.message_id,),
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        assert reopened._connection.execute(
            "SELECT typeof(message_id),message_id FROM publication_receipts"
        ).fetchone()[:2] == (
            "text",
            message.message_id,
        )


def test_publication_sql_validator_two_stores_raw_connection_and_process(db):
    c, path = db
    sql_wire_execution(c)
    envelope = sql_intent_envelope()
    statement = INSERT_INTENT
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            raw.execute(statement, envelope)
    with CoordinationStore(path) as second:
        with pytest.raises(sqlite3.IntegrityError, match="envelope is invalid"):
            second._connection.execute(statement, sql_intent_envelope(expected_id="fake"))
        assert (
            second._connection.execute(
                "SELECT coordination_validate_publication_intent(?,?,?,?,?)", envelope
            ).fetchone()[0]
            == 1
        )
    # A fresh process must install the function before a direct SQL write.
    script = """import json
import sqlite3, sys
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_schema import COORDINATION_SCHEMA_VERSION
with CoordinationStore(sys.argv[1]) as store:
    db = store._connection
    assert db.execute("SELECT coordination_validate_publication_intent("
        "'e','requester','{}','publication:v1:e:requester','not-message-id')").fetchone()[0] == 0
    try:
        db.execute("INSERT INTO publication_intents VALUES ("
            "'e','requester','{}','publication:v1:e:requester','not-message-id')")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("invalid envelope committed")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    c.execute("BEGIN IMMEDIATE")
    c.execute(statement, envelope)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")  # PENDING plus intent cannot become durable.
    c.execute("ROLLBACK")
    c.execute("BEGIN IMMEDIATE")
    c.execute(statement, envelope)
    c.execute(
        (
            "UPDATE obligations SET lifecycle=json_object('kind','publishing'),revision=2"
            " WHERE execution_id='e'"
        )
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0]
            == "publishing"
        )


def test_obligation_same_state_rewrite_is_rejected_including_terminal(db):
    c, path = db
    sql_wire_execution(c)
    with pytest.raises(sqlite3.IntegrityError, match="disposition must change"):
        c.execute("UPDATE obligations SET reason_code='rewrite',revision=2 WHERE execution_id='e'")
    c.execute(
        (
            "UPDATE obligations SET lifecycle=json_object('kind','silent'),revision=2 WHE"
            "RE execution_id='e'"
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="disposition must change"):
        c.execute("UPDATE obligations SET reason_code='rewrite',revision=3 WHERE execution_id='e'")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT state,revision FROM obligations"
        ).fetchone() == ("silent", 2)


def test_publication_intent_receipt_and_lineage_remain_frozen(db):
    c, path = db
    sql_wire_execution(c)
    body = "hello"
    message = Message(
        sender="worker",
        target="requester",
        body=body,
        type=InfoKind,
        timestamp=1.0,
        notice=False,
    )
    key = canonical_publication_key("e", "requester")
    insert_intent = INSERT_INTENT
    envelope = sql_intent_envelope(body=body)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute(insert_intent, envelope)  # PENDING cannot retain frozen intent at COMMIT.
    assert c.execute("SELECT count(*) FROM publication_intents").fetchone()[0] == 0
    c.execute("BEGIN IMMEDIATE")
    c.execute(insert_intent, envelope)
    c.execute(
        (
            "UPDATE obligations SET lifecycle=json_object('kind','publishing'),revision=2"
            " WHERE execution_id='e'"
        )
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0]
            == "publishing"
        )
        assert reopened._connection.execute(
            "SELECT typeof(message) FROM publication_intents"
        ).fetchone()[0] == "text"
        intent = PublicationIntents.one(reopened._connection, execution_id="e", exact_target="requester")
        assert intent is not None and intent.message == message
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_receipts").fetchone()[0]
            == 0
        )
    # Simulated crash/reopen checkpoint after tx1: append happens without a DB transaction.
    with pytest.raises(sqlite3.IntegrityError, match="disposition must change"):
        c.execute("UPDATE obligations SET reason_code='rewrite',revision=3 WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        PublicationReceipts(execution_id="e", exact_target="requester", seq=1,
                            message_id=message.message_id, received_at_ms=100).insert(c)
        # PUBLISHING cannot retain receipt at COMMIT.
    assert c.execute("SELECT count(*) FROM publication_receipts").fetchone()[0] == 0
    c.execute("BEGIN IMMEDIATE")
    PublicationReceipts(execution_id="e", exact_target="requester", seq=1,
                        message_id=message.message_id, received_at_ms=100).insert(c)
    c.execute(
        (
            "UPDATE obligations SET lifecycle=json_object('kind','published','message_id'"
            ",?,'seq',1),revision=3 WHERE execution_id='e'"
        ),
        (message.message_id,),
    )
    c.execute("COMMIT")
    with pytest.raises(sqlite3.IntegrityError, match="disposition must change"):
        c.execute("UPDATE obligations SET reason_code='rewrite',revision=4 WHERE execution_id='e'")
    for table in ("publication_intents", "publication_receipts", "obligations"):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE publication_intents SET message=x'41' WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE publication_receipts SET seq=2 WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot erase publication receipt"):
        c.execute(
            (
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','failed'),revisi"
                "on=2 WHERE execution_id='e'"
            )
        )
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute(
                "SELECT publication_key FROM publication_intents"
            ).fetchone()[0]
            == key
        )
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0]
            == "published"
        )


def test_recovery_and_connectivity_monotonic_reopen(db):
    c, path = db
    sql_execution(c)
    activate(c)
    c.execute("INSERT INTO connectivity VALUES ('e','connected','connected',1,100)")
    for sql in (
        "UPDATE connectivity SET revision=0 WHERE execution_id='e'",
        "UPDATE connectivity SET revision=3 WHERE execution_id='e'",
        "UPDATE connectivity SET revision=2,observed_at_ms=99 WHERE execution_id='e'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)
    c.execute("UPDATE connectivity SET revision=2,observed_at_ms=101 WHERE execution_id='e'")
    c.execute(
        "INSERT INTO recovery_audit (execution_id,kind,reason_code,"
        "sanitized_detail,attempt,elapsed_ms,observed_at_ms) "
        "VALUES ('e','model_stalled','timeout',NULL,1,100,101)"
    )
    for sql in (
        "UPDATE recovery_audit SET kind='recovered' WHERE execution_id='e'",
        "DELETE FROM recovery_audit WHERE execution_id='e'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT revision,observed_at_ms FROM connectivity"
        ).fetchone() == (2, 101)
        assert (
            reopened._connection.execute("SELECT kind FROM recovery_audit").fetchone()[0]
            == "model_stalled"
        )


@pytest.mark.parametrize(
    "table",
    [
        "schema_meta",
        "participants",
        "owner_generations",
        "executions",
        "attempts",
        "current_executions",
        "wake_claims",
        "execution_claims",
        "replay_assessments",
        "obligations",
        "publication_intents",
        "publication_receipts",
        "connectivity",
        "recovery_audit",
        "participant_aliases",
    ],
)
def test_all_durable_tables_reject_delete_even_when_empty(db, table):
    c, _path = db
    triggers = c.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND "
        "tbl_name=? AND sql LIKE '%BEFORE DELETE%'",
        (table,),
    ).fetchall()
    assert triggers, f"{table} lacks fail-loud delete authority"


def test_version_mismatch_and_symlink_rejected(tmp_path):
    path = tmp_path / "db"
    with CoordinationStore(path):
        pass
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA user_version=100")
    with pytest.raises(SchemaVersionError):
        CoordinationStore(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(IntegrityViolationError):
        CoordinationStore(link)
