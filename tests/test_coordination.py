"""Slice-1 declarations, direct-SQL authority and crash/reopen shape tests."""

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_comms.coordination import (
    ACTIVE_ATTEMPT_PHASES,
    ATTEMPT_PHASE_TRANSITIONS,
    COORDINATION_SCHEMA_VERSION,
    COORDINATION_SNAPSHOT_VERSION,
    EXECUTION_STATUS_TRANSITIONS,
    AttemptPhase,
    AttemptRecord,
    ClaimDisposition,
    CoordinationStore,
    CurrentExecutionPointer,
    ExecutionClaimLink,
    ExecutionOrigin,
    ExecutionRecord,
    ExecutionStatus,
    IntegrityViolationError,
    MessageAudience,
    ObligationState,
    OwnerFence,
    PublicationIntent,
    PublicationReceipt,
    RecoverySnapshot,
    ReplayAssessment,
    ReplayFact,
    ResponseObligation,
    SchemaVersionError,
    WakeClaim,
    WakeMode,
    attempt_phase_transition_allowed,
    attempt_retry_identity_allowed,
    canonical_publication_key,
    claim_transition_allowed,
    execution_status_transition_allowed,
    obligation_transition_allowed,
    replay_transition_allowed,
)
from agent_comms.declarations import Message, MessageType


def execution(*, status=ExecutionStatus.PENDING, ordinal=None, origin=ExecutionOrigin.ACP):
    return ExecutionRecord(
        execution_id="execution-1",
        origin=origin,
        status=status,
        owner_thread="worker",
        owner_lookup="owner-1",
        revision=1,
        current_attempt_ordinal=ordinal,
        max_attempts=3,
        reason_code=None,
        created_at_ms=100,
        updated_at_ms=100,
        exact_target="requester" if origin is ExecutionOrigin.WIRE else None,
    )


def attempt(*, ordinal=1, phase=AttemptPhase.PROMPT_STARTING, generation=1, done=False, dead=False):
    return AttemptRecord(
        execution_id="execution-1",
        attempt_ordinal=ordinal,
        owner_lookup="owner-1",
        owner_thread="worker",
        owner_generation=generation,
        owner_token_digest=f"digest-{generation}",
        phase=phase,
        revision=1,
        lease_expires_at_ms=None if phase not in ACTIVE_ATTEMPT_PHASES else 500,
        last_progress_at_ms=None,
        backend_done=done,
        process_dead=dead,
        reason_code=None,
        created_at_ms=100,
        updated_at_ms=100,
    )


def claim():
    return WakeClaim(
        claim_id="claim-1",
        recipient="worker",
        recipient_lookup="owner-1",
        wire_seq=2,
        message_id="message-1",
        exact_target="requester",
        audience=MessageAudience.DIRECT,
        wake_mode=WakeMode.FULL,
        triage_verdict=None,
        disposition=ClaimDisposition.ENGAGED,
        accepted_at_ms=100,
        updated_at_ms=100,
        revision=1,
        execution_id="execution-1",
    )


def obligation(state=ObligationState.PENDING):
    return ResponseObligation(
        execution_id="execution-1",
        exact_target="requester",
        state=state,
        reason_code=None,
        created_at_ms=100,
        updated_at_ms=100,
        revision=1,
        receipt_message_id=None,
        receipt_seq=None,
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
            (ExecutionClaimLink(record.execution_id, "claim-1", 0),)
            if record.origin is ExecutionOrigin.WIRE
            else ()
        ),
        claims=(
            (
                (
                    replace(claim(), disposition=ClaimDisposition(record.status.value))
                    if record.status
                    in {ExecutionStatus.DEFERRED, ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}
                    else claim()
                ),
            )
            if record.origin is ExecutionOrigin.WIRE
            else ()
        ),
        replay=replay or ReplayAssessment(record.execution_id, ReplayFact.NONE, True, False, 1),
        obligation=response if record.origin is ExecutionOrigin.WIRE else None,
        publication_intent=None,
        publication_receipt=None,
        connectivity=None,
        last_recovery=None,
        current_execution_id=record.execution_id if current else None,
        current_attempt_ordinal=record.current_attempt_ordinal if current else None,
        pointer_revision=1,
        is_current=current,
    )


def sql_execution(
    db,
    name="e",
    status="pending",
    origin="acp",
    target=None,
    ordinal=None,
    max_attempts=3,
    owner="p",
    thread="worker",
):
    db.execute(
        "INSERT INTO executions (execution_id,origin,status,exact_target,owner_thread,"
        "owner_lookup,revision,current_attempt_ordinal,max_attempts,reason_code,"
        "created_at_ms,updated_at_ms) VALUES (?,?,?,?,?, ?,1,?,?,NULL,0,0)",
        (name, origin, status, target, thread, owner, ordinal, max_attempts),
    )


def sql_wire_execution(db, *, pending=True, name="e", target="requester"):
    db.execute("BEGIN IMMEDIATE")
    sql_execution(db, name=name, status="queued", origin="wire", target=target)
    db.execute(
        "INSERT INTO obligations (execution_id,exact_target,state,reason_code,"
        "created_at_ms,updated_at_ms,revision,receipt_message_id,receipt_seq) "
        "VALUES (?,?,'pending',NULL,0,0,1,NULL,NULL)",
        (name, target),
    )
    db.execute(
        "INSERT INTO wake_claims (claim_id,recipient,recipient_lookup,wire_seq,"
        "message_id,exact_target,audience,wake_mode,triage_verdict,disposition,"
        "resolver_version,policy_version,accepted_at_ms,updated_at_ms,revision,"
        "execution_id) VALUES ('a','worker','p',2,'m',?,'direct','full',"
        "NULL,'engaged','resolver-v1','policy-v1',0,0,1,?)",
        (target, name),
    )
    db.execute("INSERT INTO execution_claims VALUES (?,'a',0)", (name,))
    if pending:
        db.execute(
            "UPDATE executions SET status='pending',revision=2 WHERE execution_id=?", (name,)
        )
    db.execute("COMMIT")


def sql_attempt(db, name="e", ordinal=1, generation=1, owner="p", thread="worker", digest=None):
    db.execute(
        "INSERT INTO attempts (execution_id,attempt_ordinal,owner_lookup,owner_thread,"
        "owner_generation,owner_token_digest,phase,revision,lease_expires_at_ms,"
        "last_progress_at_ms,backend_done,process_dead,reason_code,created_at_ms,"
        "updated_at_ms) VALUES (?,?,?,?,?,?,'prompt_starting',1,500,NULL,0,0,NULL,0,0)",
        (
            name,
            ordinal,
            owner,
            thread,
            generation,
            digest if digest is not None else f"digest-{generation}",
        ),
    )


def owner(db):
    db.execute("INSERT INTO participants VALUES ('p','worker',1)")
    db.execute("INSERT INTO owner_generations VALUES ('p','worker',1)")
    db.execute(
        "INSERT INTO current_executions (owner_lookup,execution_id,"
        "attempt_ordinal,pointer_revision) VALUES ('p',NULL,NULL,0)"
    )


def activate(db, name="e", ordinal=1, generation=1):
    db.execute("BEGIN IMMEDIATE")
    sql_attempt(db, name, ordinal, generation)
    db.execute(
        "UPDATE executions SET status='active',current_attempt_ordinal=?,"
        "revision=revision+1 WHERE execution_id=?",
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
        "UPDATE executions SET status='deferred',revision=revision+1 " "WHERE execution_id=?",
        (name,),
    )
    db.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=pointer_revision+1 WHERE owner_lookup='p'"
    )
    db.execute(
        "UPDATE attempts SET phase='attempt_failed',lease_expires_at_ms=NULL,"
        "backend_done=1,process_dead=1,revision=revision+1 "
        "WHERE execution_id=? AND attempt_ordinal=?",
        (name, ordinal),
    )
    db.execute("COMMIT")


def test_total_enum_partition_and_transitions():
    assert {p.value for p in ExecutionStatus} == {
        "queued",
        "pending",
        "active",
        "deferred",
        "completed",
        "failed",
    }
    assert set(EXECUTION_STATUS_TRANSITIONS) == set(ExecutionStatus)
    assert set(ATTEMPT_PHASE_TRANSITIONS) == set(AttemptPhase)
    assert AttemptPhase.RETRYING in ATTEMPT_PHASE_TRANSITIONS[AttemptPhase.ABORTING]
    assert AttemptPhase.ATTEMPT_FAILED not in ACTIVE_ATTEMPT_PHASES
    assert ExecutionStatus.ACTIVE not in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}


def test_typed_execution_and_attempt_authority_are_separate():
    pending = execution()
    active = replace(pending, status=ExecutionStatus.ACTIVE, current_attempt_ordinal=1, revision=2)
    assert execution_status_transition_allowed(pending, active)
    deferred = replace(active, status=ExecutionStatus.DEFERRED, revision=3)
    assert execution_status_transition_allowed(active, deferred)
    retry = replace(deferred, status=ExecutionStatus.ACTIVE, current_attempt_ordinal=2, revision=4)
    assert execution_status_transition_allowed(deferred, retry)
    assert not execution_status_transition_allowed(
        deferred, replace(retry, current_attempt_ordinal=3)
    )
    assert not execution_status_transition_allowed(pending, replace(active, revision=3))
    with pytest.raises(IntegrityViolationError):
        replace(pending, status=ExecutionStatus.ACTIVE)
    with pytest.raises(IntegrityViolationError):
        replace(pending, status=ExecutionStatus.COMPLETED)
    assert replace(pending, status=ExecutionStatus.FAILED).current_attempt_ordinal is None
    first = attempt()
    assert attempt_phase_transition_allowed(
        first,
        replace(
            first,
            phase=AttemptPhase.ATTEMPT_FAILED,
            lease_expires_at_ms=None,
            backend_done=True,
            process_dead=True,
            revision=2,
        ),
    )
    assert not attempt_phase_transition_allowed(
        first,
        replace(
            first,
            phase=AttemptPhase.ATTEMPT_FAILED,
            lease_expires_at_ms=None,
            backend_done=True,
            process_dead=True,
            owner_generation=2,
            revision=2,
        ),
    )
    with pytest.raises(IntegrityViolationError):
        replace(
            first, backend_done=True, phase=AttemptPhase.ATTEMPT_FAILED, lease_expires_at_ms=None
        )
    with pytest.raises(FrozenInstanceError):
        first.attempt_ordinal = 2
    with pytest.raises(FrozenInstanceError):
        pending.status = ExecutionStatus.FAILED


def test_pointer_requires_exact_composite_attempt_and_owner():
    record = execution(status=ExecutionStatus.ACTIVE, ordinal=1)
    current = CurrentExecutionPointer("owner-1", "execution-1", 1, 1)
    current.assert_matches(record, attempt())
    with pytest.raises(IntegrityViolationError):
        current.assert_matches(record, attempt(ordinal=2))
    assert current.transition_allowed(
        replace(current, execution_id=None, attempt_ordinal=None, pointer_revision=2), None, None
    )
    assert not current.transition_allowed(replace(current, pointer_revision=3), record, attempt())
    with pytest.raises(IntegrityViolationError):
        replace(current, attempt_ordinal=None)
    assert OwnerFence("execution-1", 1, "worker", 1, 1, "secret").attempt_ordinal == 1


def test_snapshot_projection_retry_and_inert_secrets():
    pre_failed = snapshot(execution_record=execution(status=ExecutionStatus.FAILED))
    assert pre_failed.can_retry is False
    failed_attempt = attempt(phase=AttemptPhase.ATTEMPT_FAILED, done=True, dead=True)
    deferred = snapshot(
        execution_record=execution(status=ExecutionStatus.DEFERRED, ordinal=1),
        attempt_record=failed_attempt,
    )
    assert deferred.can_retry
    projection = deferred.to_primitive()
    assert projection["execution"]["current_attempt_ordinal"] == 1
    assert projection["attempt"]["owner_generation"] == 1
    assert "owner_token_digest" not in str(projection)
    assert "digest-1" not in str(projection)
    with pytest.raises(IntegrityViolationError, match="budget"):
        replace(deferred.execution, max_attempts=1)
    unsafe = ReplayAssessment("execution-1", ReplayFact.TOOL_EXECUTED, False, True, 2)
    with pytest.raises(IntegrityViolationError, match="authorized retry"):
        snapshot(execution_record=deferred.execution, attempt_record=failed_attempt, replay=unsafe)
    with pytest.raises(IntegrityViolationError, match="authorized retry"):
        snapshot(
            execution_record=replace(deferred.execution, status=ExecutionStatus.FAILED),
            attempt_record=failed_attempt,
        )
    failed_unsafe = snapshot(
        execution_record=replace(deferred.execution, status=ExecutionStatus.FAILED),
        attempt_record=failed_attempt,
        replay=unsafe,
    )
    assert not failed_unsafe.can_retry
    active = snapshot(
        execution_record=execution(status=ExecutionStatus.ACTIVE, ordinal=1),
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
    assert valid.to_primitive()["claims"][0]["claim_id"] == "claim-1"
    primitive = valid.to_primitive()
    assert primitive["execution_claims"][0]["ordinal"] == 0
    assert "claim_ids" not in primitive["execution"]
    assert [item["claim_id"] for item in primitive["claims"]] == [
        item["claim_id"] for item in primitive["execution_claims"]
    ]
    with pytest.raises(IntegrityViolationError, match="ordered claims"):
        replace(valid, links=(ExecutionClaimLink(record.execution_id, "claim-1", 1),))
    with pytest.raises(IntegrityViolationError, match="ordered claims"):
        replace(valid, links=(ExecutionClaimLink("other", "claim-1", 0),))
    with pytest.raises(IntegrityViolationError):
        replace(valid, claims=(replace(claim(), recipient_lookup="other"),))
    with pytest.raises(IntegrityViolationError, match="disposition"):
        replace(valid, claims=(replace(claim(), disposition=ClaimDisposition.COMPLETED),))
    with pytest.raises(IntegrityViolationError, match="disposition"):
        replace(valid, execution=replace(record, status=ExecutionStatus.FAILED))
    with pytest.raises(IntegrityViolationError, match="authorized retry"):
        snapshot(
            execution_record=replace(
                record, status=ExecutionStatus.DEFERRED, current_attempt_ordinal=1
            ),
            attempt_record=attempt(phase=AttemptPhase.ATTEMPT_FAILED, done=True, dead=True),
            response=obligation(ObligationState.SILENT),
        )
    with pytest.raises(IntegrityViolationError):
        snapshot(
            execution_record=replace(
                record, status=ExecutionStatus.COMPLETED, current_attempt_ordinal=1
            ),
            attempt_record=attempt(phase=AttemptPhase.SUCCEEDED, done=True, dead=True),
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
        type=MessageType.INFO,
        timestamp=1.0,
        notice=False,
    )
    intent = PublicationIntent(
        execution_id="execution-1",
        sender="worker",
        exact_target="requester",
        message_type=MessageType.INFO,
        notice=False,
        timestamp=1.0,
        payload=body,
        payload_digest=hashlib.sha256(body.encode()).hexdigest(),
        publication_key=canonical_publication_key("execution-1", "requester"),
        expected_message_id=message.message_id,
    )
    assert intent.expected_message.message_id == message.message_id
    with pytest.raises(IntegrityViolationError):
        replace(intent, expected_message_id="fabricated")


def test_claim_replay_obligation_relations_remain_authoritative():
    c = claim()
    assert claim_transition_allowed(
        c, replace(c, disposition=ClaimDisposition.DEFERRED, revision=2)
    )
    assert not claim_transition_allowed(
        c, replace(c, recipient_lookup="other", revision=2, disposition=ClaimDisposition.DEFERRED)
    )
    r = ReplayAssessment("execution-1", ReplayFact.NONE, True, False, 1)
    assert replay_transition_allowed(r, replace(r, replay_safe=False, revision=2))
    with pytest.raises(IntegrityViolationError):
        replace(r, revision=2, facts=ReplayFact.TOOL_EXECUTED, replay_safe=True)
    o = obligation()
    assert obligation_transition_allowed(o, replace(o, state=ObligationState.DEFERRED, revision=2))
    assert not obligation_transition_allowed(
        o, replace(o, exact_target="wrong", state=ObligationState.DEFERRED, revision=2)
    )


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
    assert COORDINATION_SNAPSHOT_VERSION == 2
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


def test_concurrent_fresh_initializers_serialize_and_reopen(tmp_path):
    script = """import sys
from agent_comms.coordination import CoordinationStore
sys.stdin.buffer.read(1)
with CoordinationStore(sys.argv[1]) as store:
    assert store.schema_version == 2
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

    probe = """import sqlite3, sys
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
            "UPDATE executions SET status='active',"
            "current_attempt_ordinal=1,revision=2 WHERE execution_id='e'"
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
        "UPDATE executions SET status='active',"
        "current_attempt_ordinal=1,revision=2 WHERE execution_id='e'"
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
            "UPDATE attempts SET phase='attempt_failed',lease_expires_at_ms=NULL,"
            "revision=2 WHERE execution_id='e'"
        )
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "UPDATE attempts SET phase='attempt_failed',backend_done=1,process_dead=1,"
        "lease_expires_at_ms=NULL,revision=2 WHERE execution_id='e'"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")
    c.execute("ROLLBACK")
    c.execute("INSERT INTO replay_assessments VALUES ('e',0,1,0,1)")
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE executions SET status='deferred',revision=3 WHERE execution_id='e'")
    c.execute(
        "UPDATE attempts SET phase='attempt_failed',backend_done=1,process_dead=1,"
        "lease_expires_at_ms=NULL,revision=2 WHERE execution_id='e'"
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
            "SELECT phase,backend_done,process_dead " "FROM attempts"
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
        "UPDATE executions SET status='active',current_attempt_ordinal=2,revision=4 "
        "WHERE execution_id='e'"
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
        "UPDATE attempts SET phase='prompt_accepted',revision=2 "
        "WHERE execution_id='e' AND attempt_ordinal=2"
    )
    c.execute(
        "UPDATE attempts SET phase='model_running',revision=3 "
        "WHERE execution_id='e' AND attempt_ordinal=2"
    )
    c.execute(
        "UPDATE attempts SET phase='settling',revision=4 "
        "WHERE execution_id='e' AND attempt_ordinal=2"
    )
    c.execute("UPDATE executions SET status='completed',revision=5 WHERE execution_id='e'")
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=4 WHERE owner_lookup='p'"
    )
    c.execute(
        "UPDATE attempts SET phase='succeeded',backend_done=1,process_dead=1,"
        "lease_expires_at_ms=NULL,revision=5 "
        "WHERE execution_id='e' AND attempt_ordinal=2"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT status,current_attempt_ordinal " "FROM executions"
        ).fetchone() == ("completed", 2)
        assert reopened._connection.execute(
            "SELECT attempt_ordinal,phase FROM attempts " "ORDER BY attempt_ordinal"
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


@pytest.mark.parametrize("authority", ["missing", "unsafe", "budget", "published"])
def test_failed_attempt_must_settle_failed_when_retry_unauthorized(db, authority):
    c, path = db
    if authority == "published":
        sql_wire_execution(c)
        c.execute("UPDATE obligations SET state='silent',revision=2 WHERE execution_id='e'")
    else:
        sql_execution(c, max_attempts=1 if authority == "budget" else 3)
    activate(c)
    if authority == "unsafe":
        c.execute("INSERT INTO replay_assessments VALUES ('e',8,0,1,1)")
    if authority in {"budget", "published"}:
        c.execute("INSERT INTO replay_assessments VALUES ('e',0,1,0,1)")
    c.execute("BEGIN IMMEDIATE")
    if authority == "budget":
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "UPDATE executions SET status='deferred',revision=revision+1 "
                "WHERE execution_id='e'"
            )
        c.execute("ROLLBACK")
    else:
        c.execute(
            "UPDATE executions SET status='deferred',revision=revision+1 " "WHERE execution_id='e'"
        )
        if authority == "published":
            c.execute(
                "UPDATE wake_claims SET disposition='deferred',revision=2 " "WHERE claim_id='a'"
            )
        c.execute(
            "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
            "pointer_revision=2 WHERE owner_lookup='p'"
        )
        c.execute(
            "UPDATE attempts SET phase='attempt_failed',backend_done=1,process_dead=1,"
            "lease_expires_at_ms=NULL,revision=2 WHERE execution_id='e'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            c.execute("COMMIT")
        c.execute("ROLLBACK")
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE executions SET status='failed',revision=revision+1 WHERE execution_id='e'")
    if authority == "published":
        c.execute("UPDATE wake_claims SET disposition='failed',revision=2 WHERE claim_id='a'")
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute(
        "UPDATE attempts SET phase='attempt_failed',backend_done=1,process_dead=1,"
        "lease_expires_at_ms=NULL,revision=2 WHERE execution_id='e'"
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
        c.execute("UPDATE executions SET status='failed',revision=3 WHERE execution_id='e'")
    c.execute("UPDATE replay_assessments SET replay_safe=0,revision=2 WHERE execution_id='e'")
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE executions SET status='failed',revision=3 WHERE execution_id='e'")
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute(
        "UPDATE attempts SET phase='attempt_failed',backend_done=1,process_dead=1,"
        "lease_expires_at_ms=NULL,revision=2 WHERE execution_id='e'"
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
    c.execute("UPDATE executions SET status='failed',revision=3 WHERE execution_id='e'")
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute(
        "UPDATE attempts SET phase='attempt_failed',backend_done=1,process_dead=1,"
        "lease_expires_at_ms=NULL,revision=2 WHERE execution_id='e'"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        row = reopened._connection.execute(
            "SELECT * FROM executions WHERE execution_id='e'"
        ).fetchone()
        terminal = reopened._connection.execute(
            "SELECT * FROM attempts WHERE execution_id='e'"
        ).fetchone()
        assert (
            reopened._connection.execute("SELECT count(*) FROM replay_assessments").fetchone()[0]
            == 0
        )
        execution_record = ExecutionRecord(
            execution_id=row["execution_id"],
            origin=ExecutionOrigin(row["origin"]),
            status=ExecutionStatus(row["status"]),
            owner_thread=row["owner_thread"],
            owner_lookup=row["owner_lookup"],
            revision=row["revision"],
            current_attempt_ordinal=row["current_attempt_ordinal"],
            max_attempts=row["max_attempts"],
            reason_code=row["reason_code"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
            exact_target=row["exact_target"],
        )
        attempt_record = AttemptRecord(
            execution_id=terminal["execution_id"],
            attempt_ordinal=terminal["attempt_ordinal"],
            owner_lookup=terminal["owner_lookup"],
            owner_thread=terminal["owner_thread"],
            owner_generation=terminal["owner_generation"],
            owner_token_digest=terminal["owner_token_digest"],
            phase=AttemptPhase(terminal["phase"]),
            revision=terminal["revision"],
            lease_expires_at_ms=terminal["lease_expires_at_ms"],
            last_progress_at_ms=terminal["last_progress_at_ms"],
            backend_done=bool(terminal["backend_done"]),
            process_dead=bool(terminal["process_dead"]),
            reason_code=terminal["reason_code"],
            created_at_ms=terminal["created_at_ms"],
            updated_at_ms=terminal["updated_at_ms"],
        )
        absent = RecoverySnapshot(
            execution=execution_record,
            attempt=attempt_record,
            claims=(),
            links=(),
            replay=None,
            obligation=None,
            publication_intent=None,
            publication_receipt=None,
            connectivity=None,
            last_recovery=None,
            current_execution_id=None,
            current_attempt_ordinal=None,
            pointer_revision=2,
            is_current=False,
        )
        assert absent.to_primitive()["replay"] is None
        assert not absent.can_retry
        with pytest.raises(IntegrityViolationError, match="authorized retry"):
            replace(absent, execution=replace(execution_record, status=ExecutionStatus.DEFERRED))
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
        "backend_done=1,process_dead=1,revision=2,"
        "last_progress_at_ms=5,updated_at_ms=5,lease_expires_at_ms=499",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"UPDATE attempts SET {clause} WHERE execution_id='e'")
    c.execute("UPDATE attempts SET backend_done=1,revision=2 WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE attempts SET backend_done=0,revision=3 WHERE execution_id='e'")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT backend_done,revision FROM attempts"
        ).fetchone() == (1, 2)


def test_wire_execution_requires_claim_and_obligation_at_commit(db):
    c, path = db
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        sql_execution(c, origin="wire", target="requester")
    assert c.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    sql_wire_execution(c)
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM execution_claims").fetchone()[0] == 1
        )
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0] == "pending"
        )


def test_completed_wire_requires_succeeded_attempt_and_terminal_obligation(db):
    c, path = db
    sql_wire_execution(c)
    activate(c)
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE attempts SET phase='prompt_accepted',revision=2 WHERE execution_id='e'")
    c.execute("UPDATE attempts SET phase='model_running',revision=3 WHERE execution_id='e'")
    c.execute("UPDATE attempts SET phase='settling',revision=4 WHERE execution_id='e'")
    c.execute("UPDATE executions SET status='completed',revision=revision+1 WHERE execution_id='e'")
    c.execute("UPDATE wake_claims SET disposition='completed',revision=2 WHERE claim_id='a'")
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute(
        "UPDATE attempts SET phase='succeeded',lease_expires_at_ms=NULL,"
        "backend_done=1,process_dead=1,revision=5 WHERE execution_id='e'"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")
    c.execute("ROLLBACK")
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE attempts SET phase='prompt_accepted',revision=2 WHERE execution_id='e'")
    c.execute("UPDATE attempts SET phase='model_running',revision=3 WHERE execution_id='e'")
    c.execute("UPDATE attempts SET phase='settling',revision=4 WHERE execution_id='e'")
    c.execute("UPDATE obligations SET state='silent',revision=2 WHERE execution_id='e'")
    c.execute("UPDATE executions SET status='completed',revision=revision+1 WHERE execution_id='e'")
    c.execute("UPDATE wake_claims SET disposition='completed',revision=2 WHERE claim_id='a'")
    c.execute(
        "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
        "pointer_revision=2 WHERE owner_lookup='p'"
    )
    c.execute(
        "UPDATE attempts SET phase='succeeded',lease_expires_at_ms=NULL,"
        "backend_done=1,process_dead=1,revision=5 WHERE execution_id='e'"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert (
            reopened._connection.execute("SELECT status FROM executions").fetchone()[0]
            == "completed"
        )
        assert (
            reopened._connection.execute("SELECT phase FROM attempts").fetchone()[0] == "succeeded"
        )


def test_claim_execution_status_partition_is_atomic_at_commit(db):
    c, path = db
    sql_wire_execution(c, pending=False)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("UPDATE wake_claims SET disposition='failed',revision=2 WHERE claim_id='a'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("UPDATE executions SET status='failed',revision=2 WHERE execution_id='e'")
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE executions SET status='failed',revision=2 WHERE execution_id='e'")
    c.execute("UPDATE wake_claims SET disposition='failed',revision=2 WHERE claim_id='a'")
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

    def bind(identifier, seq):
        c.execute(
            "INSERT INTO wake_claims (claim_id,recipient,recipient_lookup,wire_seq,"
            "message_id,exact_target,audience,wake_mode,triage_verdict,disposition,"
            "resolver_version,policy_version,accepted_at_ms,updated_at_ms,revision,"
            "execution_id) VALUES (?,'worker','p',?,'m','requester','direct','full',"
            "NULL,'engaged','resolver-v1','policy-v1',0,0,1,'e')",
            (identifier, seq),
        )

    c.execute("BEGIN IMMEDIATE")
    bind("b", 3)
    with pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        c.execute("INSERT INTO execution_claims VALUES ('e','b',2)")
    c.execute("INSERT INTO execution_claims VALUES ('e','b',1)")
    c.execute("COMMIT")
    c.execute("UPDATE executions SET status='pending',revision=2 WHERE execution_id='e'")
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
            "SELECT claim_id,ordinal FROM execution_claims ORDER BY ordinal"
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
        "UPDATE executions SET status='active',current_attempt_ordinal=2,revision=4 "
        "WHERE execution_id='e'"
    )
    c.execute(
        "UPDATE current_executions SET execution_id='e',attempt_ordinal=2,"
        "pointer_revision=3 WHERE owner_lookup='p'"
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert reopened._connection.execute(
            "SELECT owner_generation,owner_token_digest " "FROM attempts ORDER BY attempt_ordinal"
        ).fetchall() == [(1, "digest-1"), (3, "digest-3")]


def test_fence_digest_is_globally_unique_across_executions(db):
    c, path = db
    sql_execution(c)
    activate(c)
    c.execute("INSERT INTO participants VALUES ('q','other-worker',1)")
    c.execute("INSERT INTO owner_generations VALUES ('q','other-worker',1)")
    c.execute(
        "INSERT INTO current_executions (owner_lookup,execution_id,"
        "attempt_ordinal,pointer_revision) VALUES ('q',NULL,NULL,0)"
    )
    sql_execution(c, name="other-e", owner="q", thread="other-worker")
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        sql_attempt(c, name="other-e", owner="q", thread="other-worker", digest="digest-1")
    c.execute("ROLLBACK")
    with CoordinationStore(path) as reopened:
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


def test_typed_retry_fence_relation_requires_new_global_digest():
    prior = attempt(phase=AttemptPhase.ATTEMPT_FAILED, done=True, dead=True)
    fresh = attempt(ordinal=2, generation=3)
    issued = frozenset({"digest-1", "other-execution-digest"})
    assert attempt_retry_identity_allowed(
        prior,
        fresh,
        current_owner_generation=3,
        current_owner_thread="worker",
        issued_token_digests=issued,
    )
    assert not attempt_retry_identity_allowed(
        prior,
        replace(fresh, owner_token_digest="other-execution-digest"),
        current_owner_generation=3,
        current_owner_thread="worker",
        issued_token_digests=issued,
    )
    assert not attempt_retry_identity_allowed(
        prior,
        replace(fresh, owner_token_digest="digest-1"),
        current_owner_generation=3,
        current_owner_thread="worker",
        issued_token_digests=frozenset({"other-execution-digest"}),
    )
    assert not attempt_retry_identity_allowed(
        prior,
        replace(fresh, owner_generation=1),
        current_owner_generation=1,
        current_owner_thread="worker",
        issued_token_digests=issued,
    )
    assert not attempt_retry_identity_allowed(
        prior,
        fresh,
        current_owner_generation=4,
        current_owner_thread="worker",
        issued_token_digests=issued,
    )


def test_claim_target_and_recipient_lineage_is_immutable(db):
    c, path = db
    sql_wire_execution(c, pending=False)
    c.execute("INSERT INTO participants VALUES ('other','other-worker',1)")
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "INSERT INTO wake_claims (claim_id,recipient,recipient_lookup,wire_seq,"
        "message_id,exact_target,audience,wake_mode,triage_verdict,disposition,"
        "resolver_version,policy_version,accepted_at_ms,updated_at_ms,revision,"
        "execution_id) VALUES ('b','other-worker','other',3,'m','requester',"
        "'direct','full',NULL,'engaged','resolver-v1','policy-v1',0,0,1,'e')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="claim target"):
        c.execute("INSERT INTO execution_claims VALUES ('e','b',1)")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute("COMMIT")  # Orphan execution-bound claim is not durable.
    c.execute("ROLLBACK")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE execution_claims SET ordinal=1 WHERE claim_id='a'")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert (
            reopened._connection.execute("SELECT claim_id FROM execution_claims").fetchone()[0]
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


def sql_intent_envelope(*, payload="hello", digest=None, expected_id=None):
    valid = Message(
        sender="worker",
        target="requester",
        body="hello",
        type=MessageType.INFO,
        timestamp=1.0,
        notice=False,
    )
    return (
        "e",
        "worker",
        "requester",
        "info",
        0,
        1.0,
        payload,
        (
            digest
            if digest is not None
            else hashlib.sha256(
                payload.encode("utf-8") if isinstance(payload, str) else payload
            ).hexdigest()
        ),
        canonical_publication_key("e", "requester"),
        expected_id if expected_id is not None else valid.message_id,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "blob_payload",
        "false_digest",
        "uppercase_digest",
        "wrong_digest_value",
        "invented_message_id",
        "byte_cap",
        "infinite_timestamp",
        "invalid_target",
    ],
)
def test_sql_publication_envelope_rejects_invalid_commits_and_reopens(db, mutation):
    c, path = db
    sql_wire_execution(c)
    envelope = sql_intent_envelope()
    if mutation == "blob_payload":
        envelope = sql_intent_envelope(payload=b"hello")
    elif mutation == "false_digest":
        envelope = sql_intent_envelope(digest="not-sha")
    elif mutation == "uppercase_digest":
        envelope = sql_intent_envelope(digest=envelope[7].upper())
    elif mutation == "wrong_digest_value":
        envelope = sql_intent_envelope(digest="0" * 64)
    elif mutation == "invented_message_id":
        envelope = sql_intent_envelope(expected_id="not-message-id")
    elif mutation == "byte_cap":
        envelope = sql_intent_envelope(payload="界" * 40001)
    elif mutation == "infinite_timestamp":
        envelope = (*envelope[:5], float("inf"), *envelope[6:])
    else:
        envelope = (envelope[0], "requester", *envelope[2:])  # Message forbids self-DM.
    statement = "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)"
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(statement, envelope)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(statement, envelope)
    c.execute("ROLLBACK")
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_intents").fetchone()[0]
            == 0
        )
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0] == "pending"
        )


def test_sql_invalid_utf8_cast_as_text_fails_closed(db):
    c, path = db
    sql_wire_execution(c)
    statement = (
        "INSERT INTO publication_intents VALUES ('e','worker','requester',"
        "'info',0,1.0,CAST(x'FF' AS TEXT),?,"
        "'publication:v1:e:requester',?)"
    )
    envelope = sql_intent_envelope()
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        c.execute(statement, (envelope[7], envelope[9]))
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_intents").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("payload,text", [(123, "123"), (1.25, "1.25"), (True, "1")])
def test_strict_any_publication_rejects_nontext_even_with_matching_identity(db, payload, text):
    c, path = db
    sql_wire_execution(c)
    message = Message(
        sender="worker",
        target="requester",
        body=text,
        type=MessageType.INFO,
        timestamp=1.0,
        notice=False,
    )
    envelope = (
        "e",
        "worker",
        "requester",
        "info",
        0,
        1.0,
        payload,
        hashlib.sha256(text.encode()).hexdigest(),
        canonical_publication_key("e", "requester"),
        message.message_id,
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)", envelope)
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_intents").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "field,value,text",
    [
        ("execution_id", 123, "123"),
        ("sender", 123, "123"),
        ("sender", 1.25, "1.25"),
        ("sender", True, "1"),
        ("exact_target", 123, "123"),
        ("exact_target", True, "1"),
        ("message_type", 123, "info"),
        ("payload_digest", 123, "123"),
        ("publication_key", 123, "123"),
    ],
)
def test_publication_text_columns_reject_numeric_affinity_mirrors(db, field, value, text):
    c, path = db
    name = text if field == "execution_id" else "e"
    target = text if field == "exact_target" else "requester"
    sender = text if field == "sender" else "worker"
    sql_wire_execution(c, name=name, target=target)
    message = Message(
        sender=sender,
        target=target,
        body="hello",
        type=MessageType.INFO,
        timestamp=1.0,
        notice=False,
    )
    envelope = [
        name,
        sender,
        target,
        "info",
        0,
        1.0,
        "hello",
        hashlib.sha256(b"hello").hexdigest(),
        canonical_publication_key(name, target),
        message.message_id,
    ]
    positions = {
        "execution_id": 0,
        "sender": 1,
        "exact_target": 2,
        "message_type": 3,
        "payload_digest": 7,
        "publication_key": 8,
    }
    envelope[positions[field]] = value
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)", envelope)
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
            type=MessageType.INFO,
            timestamp=1.0,
            notice=False,
        )
        if message.message_id.isdigit() and not message.message_id.startswith("0"):
            break
    else:
        raise AssertionError("no numeric-only Message ID in bounded search")
    envelope = (
        "e",
        "worker",
        "requester",
        "info",
        0,
        1.0,
        body,
        hashlib.sha256(body.encode()).hexdigest(),
        canonical_publication_key("e", "requester"),
        int(message.message_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)", envelope)
    with CoordinationStore(path) as reopened:
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_intents").fetchone()[0]
            == 0
        )
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
        (*envelope[:-1], message.message_id),
    )
    c.execute("UPDATE obligations SET state='publishing',revision=2 WHERE execution_id='e'")
    c.execute("COMMIT")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO publication_receipts VALUES ('e',1,?,100)", (int(message.message_id),)
        )
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO publication_receipts VALUES ('e',1,?,100)", (message.message_id,))
    c.execute(
        "UPDATE obligations SET state='published',revision=3,receipt_message_id=?,"
        "receipt_seq=1 WHERE execution_id='e'",
        (message.message_id,),
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        assert reopened._connection.execute(
            "SELECT typeof(message_id),message_id " "FROM publication_receipts"
        ).fetchone()[:2] == (
            "text",
            message.message_id,
        )


@pytest.mark.parametrize(
    "raw_notice,expected_notice",
    [(0, False), (1, True), (2, True), ("", False), ("false", True)],
)
def test_notice_canonicalizes_through_sql_reopen_and_snapshot(db, raw_notice, expected_notice):
    c, path = db
    sql_wire_execution(c)
    message = Message(
        sender="worker",
        target="requester",
        body="hello",
        type=MessageType.INFO,
        timestamp=1.0,
        notice=expected_notice,
    )
    digest = hashlib.sha256(b"hello").hexdigest()
    key = canonical_publication_key("e", "requester")
    intent = PublicationIntent(
        execution_id="e",
        sender="worker",
        exact_target="requester",
        message_type=MessageType.INFO,
        notice=raw_notice,
        timestamp=1.0,
        payload="hello",
        payload_digest=digest,
        publication_key=key,
        expected_message_id=message.message_id,
    )
    receipt = PublicationReceipt(
        execution_id="e",
        publication_key=key,
        seq=1,
        message_id=message.message_id,
        sender="worker",
        exact_target="requester",
        message_type=MessageType.INFO,
        notice=raw_notice,
        timestamp=1.0,
        payload_digest=digest,
    )
    assert intent.notice is expected_notice and receipt.notice is expected_notice
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "e",
            intent.sender,
            intent.exact_target,
            intent.message_type.value,
            int(intent.notice),
            intent.timestamp,
            intent.payload,
            intent.payload_digest,
            intent.publication_key,
            intent.expected_message_id,
        ),
    )
    c.execute("UPDATE obligations SET state='publishing',revision=2 WHERE execution_id='e'")
    c.execute("COMMIT")
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO publication_receipts VALUES ('e',1,?,100)", (receipt.message_id,))
    c.execute(
        "UPDATE obligations SET state='published',revision=3,receipt_message_id=?,"
        "receipt_seq=1 WHERE execution_id='e'",
        (receipt.message_id,),
    )
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        row = reopened._connection.execute("SELECT * FROM publication_intents").fetchone()
        assert row[4] == int(expected_notice) and type(row[4]) is int
        recovered_intent = PublicationIntent(
            execution_id=row[0],
            sender=row[1],
            exact_target=row[2],
            message_type=MessageType(row[3]),
            notice=row[4],
            timestamp=row[5],
            payload=row[6],
            payload_digest=row[7],
            publication_key=row[8],
            expected_message_id=row[9],
        )
        receipt_row = reopened._connection.execute(
            "SELECT seq,message_id FROM publication_receipts"
        ).fetchone()
        recovered_receipt = replace(
            receipt, seq=receipt_row[0], message_id=receipt_row[1], notice=row[4]
        )
        assert recovered_intent == intent and recovered_receipt == receipt
        assert recovered_intent.notice is expected_notice
        assert recovered_receipt.notice is expected_notice
        projected = RecoverySnapshot(
            execution=replace(
                execution(origin=ExecutionOrigin.WIRE), execution_id="e", owner_lookup="p"
            ),
            attempt=None,
            claims=(replace(claim(), execution_id="e", claim_id="a", recipient_lookup="p"),),
            links=(ExecutionClaimLink("e", "a", 0),),
            replay=None,
            obligation=replace(
                obligation(),
                execution_id="e",
                state=ObligationState.PUBLISHED,
                receipt_message_id=receipt.message_id,
                receipt_seq=1,
            ),
            publication_intent=recovered_intent,
            publication_receipt=recovered_receipt,
            connectivity=None,
            last_recovery=None,
            current_execution_id=None,
            current_attempt_ordinal=None,
            pointer_revision=0,
            is_current=False,
        )
        assert projected.publication_intent.notice is expected_notice
        assert projected.publication_receipt.notice is expected_notice


def test_integer_timestamps_canonicalize_through_message_authority(db):
    c, path = db
    sql_wire_execution(c)
    envelope = sql_intent_envelope()
    message = Message(
        sender="worker",
        target="requester",
        body="hello",
        type=MessageType.INFO,
        timestamp=1.0,
        notice=False,
    )
    typed = PublicationIntent(
        execution_id="e",
        sender="worker",
        exact_target="requester",
        message_type=MessageType.INFO,
        notice=False,
        timestamp=1,
        payload="hello",
        payload_digest=envelope[7],
        publication_key=envelope[8],
        expected_message_id=message.message_id,
    )
    assert isinstance(typed.timestamp, float) and typed.timestamp == 1.0
    receipt = PublicationReceipt(
        execution_id="e",
        publication_key=envelope[8],
        seq=1,
        message_id=message.message_id,
        sender="worker",
        exact_target="requester",
        message_type=MessageType.INFO,
        notice=False,
        timestamp=1,
        payload_digest=envelope[7],
    )
    assert isinstance(receipt.timestamp, float) and receipt.timestamp == typed.timestamp
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
        (*envelope[:5], 1, *envelope[6:]),
    )
    c.execute("UPDATE obligations SET state='publishing',revision=2 WHERE execution_id='e'")
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        stored = reopened._connection.execute(
            "SELECT timestamp,expected_message_id " "FROM publication_intents"
        ).fetchone()
        assert (stored[0], stored[1]) == (1.0, message.message_id)


def test_signed_zero_timestamps_canonicalize_through_message_authority(db):
    c, path = db
    sql_wire_execution(c)
    message = Message(
        sender="worker",
        target="requester",
        body="hello",
        type=MessageType.INFO,
        timestamp=0.0,
        notice=False,
    )
    digest = hashlib.sha256(b"hello").hexdigest()
    key = canonical_publication_key("e", "requester")
    intent = PublicationIntent(
        execution_id="e",
        sender="worker",
        exact_target="requester",
        message_type=MessageType.INFO,
        notice=False,
        timestamp=-0.0,
        payload="hello",
        payload_digest=digest,
        publication_key=key,
        expected_message_id=message.message_id,
    )
    assert repr(intent.timestamp) == "0.0"
    receipt = PublicationReceipt(
        execution_id="e",
        publication_key=key,
        seq=1,
        message_id=message.message_id,
        sender="worker",
        exact_target="requester",
        message_type=MessageType.INFO,
        notice=False,
        timestamp=-0.0,
        payload_digest=digest,
    )
    assert repr(receipt.timestamp) == "0.0"
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("e", "worker", "requester", "info", 0, -0.0, "hello", digest, key, message.message_id),
    )
    c.execute("UPDATE obligations SET state='publishing',revision=2 WHERE execution_id='e'")
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        row = reopened._connection.execute(
            "SELECT timestamp,expected_message_id " "FROM publication_intents"
        ).fetchone()
        assert repr(row[0]) == "0.0" and row[1] == message.message_id


def test_publication_sql_validator_two_stores_raw_connection_and_process(db):
    c, path = db
    sql_wire_execution(c)
    envelope = sql_intent_envelope()
    statement = "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)"
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            raw.execute(statement, envelope)
    with CoordinationStore(path) as second:
        with pytest.raises(sqlite3.IntegrityError, match="envelope is invalid"):
            second._connection.execute(statement, sql_intent_envelope(digest="fake"))
        assert (
            second._connection.execute(
                "SELECT coordination_validate_publication_intent(?,?,?,?,?,?,?,?,?,?)", envelope
            ).fetchone()[0]
            == 1
        )
    # A fresh process must install the function before a direct SQL write.
    script = """import sqlite3, sys
from agent_comms.coordination import CoordinationStore
with CoordinationStore(sys.argv[1]) as store:
    db = store._connection
    assert db.execute("SELECT coordination_validate_publication_intent("
        "'e','worker','requester','info',0,1.0,'hello',"
        "'not-sha','publication:v1:e:requester','not-message-id')").fetchone()[0] == 0
    try:
        db.execute("INSERT INTO publication_intents VALUES ("
            "'e','worker','requester','info',0,1.0,'hello',"
            "'not-sha','publication:v1:e:requester','not-message-id')")
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
    c.execute("UPDATE obligations SET state='publishing',revision=2 WHERE execution_id='e'")
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
    c.execute("UPDATE obligations SET state='silent',revision=2 WHERE execution_id='e'")
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
        type=MessageType.INFO,
        timestamp=1.0,
        notice=False,
    )
    key = canonical_publication_key("e", "requester")
    insert_intent = "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)"
    envelope = (
        "e",
        "worker",
        "requester",
        "info",
        0,
        1.0,
        body,
        hashlib.sha256(body.encode()).hexdigest(),
        key,
        message.message_id,
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute(insert_intent, envelope)  # PENDING cannot retain frozen intent at COMMIT.
    assert c.execute("SELECT count(*) FROM publication_intents").fetchone()[0] == 0
    c.execute("BEGIN IMMEDIATE")
    c.execute(insert_intent, envelope)
    c.execute("UPDATE obligations SET state='publishing',revision=2 WHERE execution_id='e'")
    c.execute("COMMIT")
    with CoordinationStore(path) as reopened:
        reopened._connection.row_factory = None
        assert (
            reopened._connection.execute("SELECT state FROM obligations").fetchone()[0]
            == "publishing"
        )
        assert reopened._connection.execute(
            "SELECT typeof(payload),payload FROM " "publication_intents"
        ).fetchone() == ("text", body)
        assert (
            reopened._connection.execute("SELECT count(*) FROM publication_receipts").fetchone()[0]
            == 0
        )
    # Simulated crash/reopen checkpoint after tx1: append happens without a DB transaction.
    with pytest.raises(sqlite3.IntegrityError, match="disposition must change"):
        c.execute("UPDATE obligations SET reason_code='rewrite',revision=3 WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        c.execute(
            "INSERT INTO publication_receipts VALUES ('e',1,?,100)", (message.message_id,)
        )  # PUBLISHING cannot retain receipt at COMMIT.
    assert c.execute("SELECT count(*) FROM publication_receipts").fetchone()[0] == 0
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO publication_receipts VALUES ('e',1,?,100)", (message.message_id,))
    c.execute(
        "UPDATE obligations SET state='published',revision=3,receipt_message_id=?,"
        "receipt_seq=1 WHERE execution_id='e'",
        (message.message_id,),
    )
    c.execute("COMMIT")
    with pytest.raises(sqlite3.IntegrityError, match="disposition must change"):
        c.execute("UPDATE obligations SET reason_code='rewrite',revision=4 WHERE execution_id='e'")
    for table in ("publication_intents", "publication_receipts", "obligations"):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE publication_intents SET payload=x'41' WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE publication_receipts SET seq=2 WHERE execution_id='e'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot erase publication receipt"):
        c.execute("UPDATE executions SET status='failed',revision=2 WHERE execution_id='e'")
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
