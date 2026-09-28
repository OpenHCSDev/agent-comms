"""Replay the pre-S3 declarations against current declarations without live data.

Runs in this worktree with PYTHONPATH=src and the existing integration venv.
The old source is read from git, never checked out over a shared file.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from itertools import product
from pathlib import Path

from agent_comms import coordination as current
from agent_comms.coordination_store import MutationStore


def legacy():
    source = subprocess.check_output(
        ["git", "show", "cb54e156f6fc1b4e7ce87e9d8fdfbd5ee4c25321:src/agent_comms/coordination.py"],
        text=True,
    )
    spec = importlib.util.spec_from_loader("agent_comms._s3_legacy", loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(compile(source, "<pre-S3 coordination>", "exec"), module.__dict__)
    return module


def build(c, status, phase, origin):
    ordinal = 1 if phase else None
    target = "requester" if origin == "wire" else None
    execution = c.ExecutionRecord(
        execution_id="e",
        origin=c.ExecutionOrigin(origin),
        status=c.ExecutionStatus(status),
        owner_thread="worker",
        owner_lookup="p",
        revision=1,
        current_attempt_ordinal=ordinal,
        max_attempts=2,
        reason_code=None,
        created_at_ms=0,
        updated_at_ms=0,
        exact_target=target,
    )
    attempt = None
    if phase:
        terminal = phase in ("succeeded", "attempt_failed")
        attempt = c.AttemptRecord(
            execution_id="e",
            attempt_ordinal=1,
            owner_lookup="p",
            owner_thread="worker",
            owner_generation=1,
            owner_token_digest="secret-digest",
            phase=c.AttemptPhase(phase),
            revision=1,
            lease_expires_at_ms=None if terminal else 500,
            last_progress_at_ms=None,
            backend_done=terminal,
            process_dead=terminal,
            reason_code=None,
            created_at_ms=0,
            updated_at_ms=0,
        )
    response = None
    claims = ()
    if target:
        claim_state = "engaged" if status in ("queued", "pending", "active") else status
        claims = (
            c.WakeClaim(
                claim_id="claim",
                recipient="worker",
                recipient_lookup="p",
                wire_seq=1,
                message_id="message",
                exact_target=target,
                audience=c.MessageAudience.DIRECT,
                wake_mode=c.WakeMode.FULL,
                triage_verdict=None,
                disposition=c.ClaimDisposition(claim_state),
                accepted_at_ms=0,
                updated_at_ms=0,
                execution_id="e",
            ),
        )
        state = (
            "silent"
            if status == "completed"
            else status
            if status in ("failed", "deferred")
            else "pending"
        )
        response = c.ResponseObligation(
            execution_id="e",
            exact_target=target,
            state=c.ObligationState(state),
            reason_code=None,
            created_at_ms=0,
            updated_at_ms=0,
            revision=1,
        )
    active = status == "active"
    return c.RecoverySnapshot(
        execution,
        attempt,
        claims,
        (c.ExecutionClaimLink("e", "claim", 0),) if claims else (),
        c.ReplayAssessment("e", c.ReplayFact.NONE, status != "failed", status == "failed", 1),
        response,
        None,
        None,
        None,
        None,
        "e" if active else None,
        ordinal if active else None,
        1,
        active,
    )


def main():
    old = legacy()
    replayed = []
    phases = [p.value for p in old.AttemptPhase if p in old.ACTIVE_ATTEMPT_PHASES]
    cases = [
        ("queued", None),
        ("pending", None),
        ("deferred", None),
        ("failed", None),
        ("completed", "succeeded"),
        ("deferred", "attempt_failed"),
        ("failed", "attempt_failed"),
    ]
    cases += [("active", phase) for phase in phases]
    for origin in old.ExecutionOrigin:
        for status, phase in cases:
            before = build(old, status, phase, origin.value).to_primitive()
            after = build(current, status, phase, origin.value).to_primitive()
            assert before == after, (origin, status, phase)
            assert "secret-digest" not in json.dumps(after)
            replayed.append([origin.value, status, phase])
    legality_cases = 0
    for disposition, mode, verdict, execution, target in product(
        [m.value for m in old.ClaimDisposition],
        [m.value for m in old.WakeMode],
        [None, "ignore", "engage"],
        [None, "e"],
        [None, "target"],
    ):
        outcomes = []
        for c in (old, current):
            try:
                c.WakeClaim(
                    claim_id="claim",
                    recipient="worker",
                    recipient_lookup="p",
                    wire_seq=1,
                    message_id="m",
                    exact_target=target,
                    audience=c.MessageAudience.DIRECT,
                    wake_mode=c.WakeMode(mode),
                    triage_verdict=verdict,
                    disposition=c.ClaimDisposition(disposition),
                    accepted_at_ms=0,
                    updated_at_ms=0,
                    execution_id=execution,
                )
            except (ValueError, TypeError, c.IntegrityViolationError):
                outcomes.append(False)
            else:
                outcomes.append(True)
        assert outcomes[0] == outcomes[1], (disposition, mode, verdict, execution, target, outcomes)
        legality_cases += 1
    for state, message, seq in product(
        [m.value for m in old.ObligationState], [None, "m"], [None, 0, 1]
    ):
        outcomes = []
        for c in (old, current):
            try:
                c.ResponseObligation(
                    execution_id="e",
                    exact_target="target",
                    state=c.ObligationState(state),
                    reason_code=None,
                    created_at_ms=0,
                    updated_at_ms=0,
                    revision=1,
                    receipt_message_id=message,
                    receipt_seq=seq,
                )
            except (ValueError, TypeError, c.IntegrityViolationError):
                outcomes.append(False)
            else:
                outcomes.append(True)
        assert outcomes[0] == outcomes[1], (state, message, seq, outcomes)
        legality_cases += 1
    # Reopen an actual database made by the old schema; prove the new reader
    # preserves it without a schema rewrite or migration of persisted strings.
    with tempfile.TemporaryDirectory(
        dir=Path(".artifacts/s3"), prefix="legacy-store-"
    ) as directory:
        path = Path(directory) / "coordination.sqlite3"
        with old.CoordinationStore(path) as store:
            db = store._connection
            db.execute("INSERT INTO participants VALUES ('p','worker',1)")
            db.execute("INSERT INTO owner_generations VALUES ('p','worker',1)")
            db.execute("INSERT INTO current_executions VALUES ('p',NULL,NULL,0)")
            db.execute(
                "INSERT INTO executions(execution_id,origin,status,owner_thread,"
                "owner_lookup,revision,"
                "current_attempt_ordinal,max_attempts,reason_code,created_at_ms,updated_at_ms) "
                "VALUES ('e','acp','pending','worker','p',1,NULL,2,NULL,0,0)"
            )
            schema = tuple(
                tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")
            )
        with MutationStore(path) as store:
            assert store.snapshot("e").execution.status.value == "pending"
            assert (
                tuple(
                    tuple(row)
                    for row in store._connection.execute(
                        "SELECT name,sql FROM sqlite_master ORDER BY name"
                    )
                )
                == schema
            )
    result = {
        "snapshot_cases": len(replayed),
        "claim_and_receipt_legality_cases": legality_cases,
        "cases": replayed,
        "legacy_database_reopened_without_schema_rewrite": True,
    }
    Path("evidence/s3/replay-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"{len(replayed)} snapshot projections and {legality_cases} legality cases identical; "
        "legacy database reopened without schema rewrite"
    )


if __name__ == "__main__":
    main()
