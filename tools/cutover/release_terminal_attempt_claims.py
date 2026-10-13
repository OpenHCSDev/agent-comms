#!/usr/bin/env python3
"""One-time exact release of claims stranded after terminal attempt settlement.

First run without ``--apply`` against a copied and resealed private root. That
trial invokes the real #780 release owner and writes the exact changed-claim
manifest. Review it, then pass the same manifest with ``--apply`` to the source
root. Any failed append has UNKNOWN outcome; never retry without inspecting the
bus. The tool does not alter coordinator SQL, registry state, input records, or
attempt disposition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

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


def _fixed_store_digests(root: Path) -> dict[Path, str]:
    protected = (
        root / "coordination.sqlite3",
        root / "registry.json",
        root / "input_dispositions.json",
        root / "owner_release_receipts.json",
    )
    return {path: _digest(path) for path in protected if path.exists()}


def _manifest(snapshot, claims: tuple[ClaimOwner, ...]) -> TerminalClaimManifest:
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
        claims=tuple(ExactClaim.from_owner(claim) for claim in sorted(
            claims, key=lambda claim: (claim.incarnation, claim.resource)
        )),
    )


def repair(
    root: Path,
    execution_id: str,
    expected: TerminalClaimManifest | None = None,
) -> tuple[TerminalClaimManifest, dict[str, object]]:
    """Use the settlement owner, then prove its exact claim-table delta.

    ``expected is None`` is the destructive trial intended only for a copied
    store set. Supplying the resulting manifest requires the source root to
    have every unchanged exact claim before the same owner is invoked there.
    """
    database = root / "coordination.sqlite3"
    fixed = _fixed_store_digests(root)
    with Coordination(str(database)) as store, store.session.boundary() as boundary:
        snapshot = store.snapshots.get(execution_id)
        terminal = _manifest(snapshot, ())
        if expected is not None:
            if terminal != TerminalClaimManifest(
                expected.execution_id,
                expected.attempt_ordinal,
                expected.owner_lookup,
                expected.participant_generation,
                expected.phase,
                (),
            ):
                raise RecoveryBlocked("terminal attempt differs from the copied-store trial")
            if not expected.claims:
                raise RecoveryBlocked("copied-store trial did not release any claims")
        metadata = boundary.bus.log._private_marker_unlocked()
        before_rows, before_seq = boundary.bus_lock.claim_transitions(boundary.bus.log, metadata)
        before = project_verified_transitions(before_rows)
        if expected is not None:
            for claim in expected.claims:
                current = before.get(claim.resource)
                if current is None or ExactClaim.from_owner(current) != claim:
                    raise RecoveryBlocked("exact copied-store claim is absent or changed")

        # This is the only selection and release decision. It is the same owner
        # used by normal #780 attempt-ending settlement, not a cutover copy.
        snapshot.release_resources(boundary)

        metadata = boundary.bus.log._private_marker_unlocked()
        after_rows, after_seq = boundary.bus_lock.claim_transitions(boundary.bus.log, metadata)
        after = project_verified_transitions(after_rows)
        removed = tuple(before[resource] for resource in before.keys() - after.keys())
        unchanged = {resource: claim for resource, claim in before.items() if resource in after}
        if unchanged != dict(after):
            raise RecoveryBlocked("claim repair changed a generation instead of releasing it")
        actual = _manifest(snapshot, removed)
        if expected is not None and actual != expected:
            raise RecoveryBlocked("source-root releases differ from the copied-store trial")
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
    if _fixed_store_digests(root) != fixed:
        raise RecoveryBlocked("claim repair changed protected private-store state")
    return actual, {
        "execution_id": execution_id,
        "released": len(actual.claims),
        "before_seq": before_seq,
        "after_seq": after_seq,
        "unchanged_claims": len(after),
        "protected_stores_unchanged": True,
        "trial": expected is None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve(strict=True)
    expected = TerminalClaimManifest.read(args.manifest) if args.apply else None
    if expected is not None and expected.execution_id != args.execution:
        raise SystemExit("manifest execution differs from --execution")
    actual, result = repair(root, args.execution, expected)
    if not args.apply:
        actual.write(args.manifest)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
