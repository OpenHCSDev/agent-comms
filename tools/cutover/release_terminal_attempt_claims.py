#!/usr/bin/env python3
"""One-time exact release of claims stranded after terminal attempt settlement.

``--plan`` reads a certified root without repairing or publishing anything and
writes the exact proposed manifest outside that root. ``--apply`` requires that
manifest, refuses any release-set drift before publication, then uses the #780
release owner. No default destructive trial mode exists. An append failure has
UNKNOWN outcome; never retry without inspecting the bus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from contextlib import ExitStack
import sqlite3
from pathlib import Path

from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordinator import Coordination
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.store_files import _store_lock
from agent_comms.private_bus_checkpoint import CheckpointNeedsRepair
from agent_comms.wire_log import WireLog
from agent_comms.envelope_claim_transitions import ClaimOwner, project_verified_transitions


@dataclass(frozen=True)
class ExactClaim:
    resource: str
    generation: str
    owner: str
    incarnation: str
    seq: int
    message_id: str

    @classmethod
    def from_owner(cls, claim: ClaimOwner) -> ExactClaim:
        return cls(
            claim.resource,
            claim.generation,
            claim.owner,
            claim.incarnation,
            claim.seq,
            claim.message_id,
        )


@dataclass(frozen=True)
class TerminalClaimManifest:
    execution_id: str
    attempt_ordinal: int
    owner_lookup: str
    participant_generation: int
    phase: str
    claims: tuple[ExactClaim, ...]

    @classmethod
    def read(cls, path: Path) -> TerminalClaimManifest:
        raw = json.loads(path.read_text())
        return cls(
            execution_id=raw["execution_id"],
            attempt_ordinal=raw["attempt_ordinal"],
            owner_lookup=raw["owner_lookup"],
            participant_generation=raw["participant_generation"],
            phase=raw["phase"],
            claims=tuple(ExactClaim(**row) for row in raw["claims"]),
        )

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixed_store_digests(root: Path, db: sqlite3.Connection) -> dict[str, str]:
    # Never open/close another fd for this SQLite inode while holding its read
    # transaction: POSIX close can release the connection's process-wide locks.
    protected = ("registry.json", "input_dispositions.json", "owner_release_receipts.json")
    return {
        "coordination.sqlite3": hashlib.sha256(db.serialize()).hexdigest(),
        **{name: _digest(root / name) for name in protected if (root / name).exists()},
    }


def _selected(attempt, claims) -> tuple[ClaimOwner, ...]:
    # Preflight the complete set selected by RecoverySnapshot.release_resources,
    # not just the subset named in the manifest. Publication remains its work.
    return tuple(claim for claim in claims if claim.admission is not None
                 and claim.admission.execution_id == attempt.execution_id
                 and claim.admission.attempt_ordinal == attempt.attempt_ordinal
                 and claim.admission.recipient_lookup == attempt.owner_lookup
                 and claim.admission.participant_generation == attempt.owner_generation)


def _manifest(attempt, claims: tuple[ClaimOwner, ...]) -> TerminalClaimManifest:
    if not (
        attempt.lifecycle.terminal
        and attempt.lifecycle.backend_done
        and attempt.lifecycle.process_dead
    ):
        raise RecoveryBlocked(
            "historical claim repair requires a terminal attempt with final done/death evidence"
        )
    return TerminalClaimManifest(
        execution_id=attempt.execution_id,
        attempt_ordinal=attempt.attempt_ordinal,
        owner_lookup=attempt.owner_lookup,
        participant_generation=attempt.owner_generation,
        phase=type(attempt.lifecycle).declared_name,
        claims=tuple(ExactClaim.from_owner(claim) for claim in sorted(
            claims, key=lambda claim: (claim.incarnation, claim.resource)
        )),
    )


def plan(root: Path, execution_id: str) -> TerminalClaimManifest:
    """Shared physical locks and read-only SQL; no checkpoint repair or append."""
    # These must already exist: a read-only plan must not create lock files.
    for name in ("wire", "bus.jsonl", "registry.json"):
        if not (root / f".{name}.lock").is_file():
            raise RecoveryBlocked(f"existing {name} lock is required for read-only planning")
    log = WireLog(root / "bus.jsonl")
    with (
        _store_lock(root / "wire", shared=True, blocking=False),
        _store_lock(log.path, shared=True, blocking=False) as bus_lock,
        _store_lock(root / "registry.json", shared=True, blocking=False),
        CoordinationStore.observing(root / "coordination.sqlite3", lock_timeout=0) as db,
    ):
        execution = ExecutionRecord.one(db, execution_id=execution_id)
        if execution is None or execution.lifecycle.current_attempt_ordinal is None:
            raise RecoveryBlocked("execution has no recorded attempt")
        attempt = AttemptRecord.one(db, execution_id=execution_id,
                                    attempt_ordinal=execution.lifecycle.current_attempt_ordinal)
        if attempt is None:
            raise RecoveryBlocked("recorded attempt is absent")
        metadata = log._private_marker_unlocked()
        rows, _ = bus_lock.claim_transitions(log, metadata)
        claims = project_verified_transitions(rows)
        return _manifest(attempt, _selected(attempt, claims.values()))


def repair(
    root: Path,
    execution_id: str,
    expected: TerminalClaimManifest,
) -> tuple[TerminalClaimManifest, dict[str, object]]:
    """Refuse release-set drift before append; verify while all locks remain held."""
    if expected.execution_id != execution_id or not expected.claims:
        raise RecoveryBlocked("a nonempty matching plan is required")
    # Refuse a stale manifest before Coordination or NativeBoundary can create,
    # initialize or recover any store. plan() accepts only pre-existing lock
    # files and holds shared locks throughout its certified, read-only view.
    try:
        observed = plan(root, execution_id)
    except CheckpointNeedsRepair as error:
        raise RecoveryBlocked(
            "read-only preflight requires a final checkpoint; nothing changed"
        ) from error
    if observed != expected:
        raise RecoveryBlocked("complete release set differs from the plan; nothing changed")
    database = root / "coordination.sqlite3"
    with (
        Coordination(str(database), lock_timeout=0) as store,
        store.session.boundary(blocking=False) as boundary,
        ExitStack() as protected,
    ):
        for name in ("input_dispositions.json", "owner_release_receipts.json"):
            protected.enter_context(_store_lock(root / name, shared=True, blocking=False))
        # The SQL read transaction prevents another writer committing while the
        # bus and protected-file locks cover the entire before/release/after span.
        protected.enter_context(store.session.read())
        db = store.session._connection
        snapshot = store.snapshots.get(execution_id)
        attempt = snapshot.require_attempt()
        metadata = boundary.bus.log._private_marker_unlocked()
        before_rows, before_seq = boundary.bus_lock.claim_transitions(boundary.bus.log, metadata)
        before = project_verified_transitions(before_rows)
        actual = _manifest(attempt, _selected(attempt, before.values()))
        if actual != expected:
            raise RecoveryBlocked("complete release set differs from the plan; nothing appended")
        fixed = _fixed_store_digests(root, db)
        snapshot.release_resources(boundary)

        metadata = boundary.bus.log._private_marker_unlocked()
        after_rows, after_seq = boundary.bus_lock.claim_transitions(boundary.bus.log, metadata)
        after = project_verified_transitions(after_rows)
        removed = tuple(before[resource] for resource in before.keys() - after.keys())
        unchanged = {resource: claim for resource, claim in before.items() if resource in after}
        if unchanged != dict(after):
            raise RecoveryBlocked("claim repair changed a generation instead of releasing it")
        actual = _manifest(attempt, removed)
        if actual != expected:
            raise RecoveryBlocked("actual releases differ from the plan")
        if not actual.claims:
            raise RecoveryBlocked("terminal release owner changed no claims")
        appended = after_rows[len(before_rows):]
        released = tuple(
            (release.resource, release.generation)
            for row in appended
            for release in row.releases
        )
        wanted = tuple(sorted((claim.resource, claim.generation) for claim in actual.claims))
        if (
            any(row.claims or row.admission is not None for row in appended)
            or tuple(sorted(released)) != wanted
        ):
            raise RecoveryBlocked("claim repair appended anything except exact releases")
        if _fixed_store_digests(root, db) != fixed:
            raise RecoveryBlocked("claim repair changed protected private-store state")
    return actual, {
        "execution_id": execution_id,
        "released": len(actual.claims),
        "before_seq": before_seq,
        "after_seq": after_seq,
        "unchanged_claims": len(after),
        "protected_stores_unchanged": True,
        "applied": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve(strict=True)
    if args.manifest.resolve().is_relative_to(root):
        raise SystemExit("manifest must be outside the bus root")
    if args.plan:
        actual = plan(root, args.execution)
        actual.write(args.manifest)
        result = {"planned": len(actual.claims), "applied": False,
                  "manifest": str(args.manifest)}
    else:
        expected = TerminalClaimManifest.read(args.manifest)
        _, result = repair(root, args.execution, expected)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
