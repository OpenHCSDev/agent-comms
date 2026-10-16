"""Carry the claim index once, with old writers stopped; delete after cutover.

Default is a read-only plan. The existing canonical installer certifies the
unchanged bus and atomically replaces its checkpoint/seal. No old row decoder
or reader fallback is installed in production source.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path

from agent_comms.child_process import Platform
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.private_bus_checkpoint import (
    CheckpointTable, ClaimCommits, PrefixCertificate, ResourceClaims, _path,
)
from agent_comms.private_path import FileRevision
from agent_comms.store_files import _store_lock_file
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable
from agent_comms.wire_log import WireLog


def carry(bus_path: Path, *, apply: bool = False) -> dict:
    bus = WireLog(bus_path)
    if bus.path.name != "bus.jsonl" or not bus.path.is_file():
        raise ValueError("An existing canonical private bus.jsonl is required")
    checkpoint = _path(bus.path)
    platform = Platform.current()
    # The new ordinary reader intentionally rejects the old schema. Borrow
    # only the existing physical lock implementation for this offline carry.
    with _store_lock_file(bus.path) as lock_file:
        platform.acquire_store_lock(lock_file.fileno(), shared=False, blocking=False)
        try:
            marker = bus._private_marker_unlocked()
            if not marker.claims:
                raise RelationViolationError("Resource claim carry requires a private claim bus")
            with closing(CoordinationStore.read_snapshot(checkpoint)) as db:
                original = PrefixCertificate.read_witness(db)
                marker.seal.check_final(original, checkpoint)
                original.require_marker(marker)
                if FileRevision.from_stat(bus.path.stat()) != original.revision:
                    raise RelationViolationError("Bus differs from its sealed original prefix")
                actual = SQLiteSchemaObject.read(db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"
                ))
            target = {name: sql for row in TypedTable.members_with(CheckpointTable)
                      for name, sql in row.schema_objects().items()}
            additions = {name: sql for row in (ClaimCommits, ResourceClaims)
                         for name, sql in row.schema_objects().items()}
            preceding = {name: sql for name, sql in target.items() if name not in additions}
            already_carried = not SQLiteSchemaObject.differences(actual, target)
            if not already_carried and SQLiteSchemaObject.differences(actual, preceding):
                raise RelationViolationError("Foreign, changed or partially carried checkpoint schema")
            plan = {
                "bus": str(bus.path), "through_sequence": original.through_seq,
                "bus_bytes": original.offset, "original_digest": original.digest,
                "new_objects": [] if already_carried else sorted(additions),
                "already_carried": already_carried, "applied": False,
                "bus_bytes_rewritten": 0,
            }
            if not apply or already_carried:
                return plan
            expected_marker = replace(marker, checkpoint_version=None, checkpoint_seal=None)
            try:
                carried = PrefixCertificate._install(bus, marker)
                current_marker = bus._private_marker_unlocked()
                if (carried != original or
                    replace(current_marker, checkpoint_version=None, checkpoint_seal=None) != expected_marker):
                    raise RelationViolationError("Carry changed original bytes or non-seal marker facts")
            except Exception as error:
                raise RelationViolationError(
                    "Claim-index carry outcome UNKNOWN; preserve this root, do not retry"
                ) from error
            plan["applied"] = True
            return plan
        finally:
            platform.release_store_lock(lock_file.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(carry(args.bus, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
