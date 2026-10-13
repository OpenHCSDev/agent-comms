#!/usr/bin/env python3
"""One-time exact release of claims stranded after terminal attempt settlement.

Run against a copied private root first. Inspect emits the complete exact claim
set. Apply requires that saved manifest and refuses any drift. A failed append
has UNKNOWN outcome and this command must not be retried without inspecting the
bus. This tool never changes coordinator SQL, registry state, input records, or
attempt disposition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordinator import Coordination
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


def _matching_claims(snapshot, claims: Sequence[ClaimOwner]) -> tuple[ClaimOwner, ...]:
    attempt = snapshot.require_attempt()
    return tuple(sorted(
        (
            claim for claim in claims
            if claim.admission is not None
            and claim.admission.execution_id == attempt.execution_id
            and claim.admission.attempt_ordinal == attempt.attempt_ordinal
            and claim.admission.recipient_lookup == attempt.owner_lookup
            and claim.admission.participant_generation == attempt.owner_generation
        ),
        key=lambda claim: (claim.incarnation, claim.resource),
    ))


def _manifest(snapshot, claims: Sequence[ClaimOwner]) -> TerminalClaimManifest:
    attempt = snapshot.require_attempt()
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
        claims=tuple(ExactClaim.from_owner(claim) for claim in _matching_claims(snapshot, claims)),
    )


def inspect(root: Path, execution_id: str) -> TerminalClaimManifest:
    database = root / "coordination.sqlite3"
    with Coordination(str(database)) as store, store.session.boundary() as boundary:
        snapshot = store.snapshots.get(execution_id)
        metadata = boundary.bus.log._private_marker_unlocked()
        transitions, _ = boundary.bus_lock.claim_transitions(boundary.bus.log, metadata)
        return _manifest(snapshot, tuple(project_verified_transitions(transitions).values()))


def apply(root: Path, expected: TerminalClaimManifest) -> dict[str, object]:
    """Release exactly the unchanged manifest, then prove the claim-table delta."""
    database = root / "coordination.sqlite3"
    registry = root / "registry.json"
    fixed = {path: _digest(path) for path in (database, registry)}
    with Coordination(str(database)) as store, store.session.boundary() as boundary:
        snapshot = store.snapshots.get(expected.execution_id)
        metadata = boundary.bus.log._private_marker_unlocked()
        before_rows, before_seq = boundary.bus_lock.claim_transitions(boundary.bus.log, metadata)
        before = project_verified_transitions(before_rows)
        actual = _manifest(snapshot, tuple(before.values()))
        if actual != expected or not actual.claims:
            raise RecoveryBlocked("terminal claim manifest is absent or changed")
        snapshot.release_resources(boundary)
        metadata = boundary.bus.log._private_marker_unlocked()
        after_rows, after_seq = boundary.bus_lock.claim_transitions(boundary.bus.log, metadata)
        after = project_verified_transitions(after_rows)
        expected_after = dict(before)
        for claim in actual.claims:
            current = expected_after.get(claim.resource)
            if current is None or ExactClaim.from_owner(current) != claim:
                raise RecoveryBlocked("terminal claim changed before release verification")
            del expected_after[claim.resource]
        if dict(after) != expected_after:
            raise RecoveryBlocked("claim repair changed resources outside the exact manifest")
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
    changed = {path: _digest(path) for path in (database, registry)}
    if changed != fixed:
        raise RecoveryBlocked("claim repair changed coordinator SQL or registry state")
    return {
        "execution_id": expected.execution_id,
        "released": len(expected.claims),
        "before_seq": before_seq,
        "after_seq": after_seq,
        "unchanged_claims": len(expected_after),
        "sql_and_registry_unchanged": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve(strict=True)
    if args.apply:
        expected = TerminalClaimManifest.read(args.manifest)
        if expected.execution_id != args.execution:
            raise SystemExit("manifest execution differs from --execution")
        print(json.dumps(apply(root, expected), sort_keys=True))
    else:
        inspect(root, args.execution).write(args.manifest)
        print(json.dumps({"manifest": str(args.manifest), "applied": False}, sort_keys=True))


if __name__ == "__main__":
    main()
