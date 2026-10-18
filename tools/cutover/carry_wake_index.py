"""Carry the unchanged wake rows into the expanded wake-policy declaration."""
from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.store_files import _store_lock
from agent_comms.wake_candidate_index import CandidateCheckpoint, WakeCandidateIndex
from agent_comms.wire_log import CurrentSnapshotOpening, WireLog


def carry(root: Path, source: Path, *, apply: bool = False) -> dict:
    log = WireLog(root / "bus.jsonl")
    index = WakeCandidateIndex(log)
    with closing(CoordinationStore.read_snapshot(source)) as db:
        expected = index.declared_schema()
        actual = {item.name: item.sql for item in index._schema(db)}
        if actual.keys() != expected.keys():
            raise ValueError("Wake index has unknown schema objects")
        for name, sql in actual.items():
            expanded = sql.replace("'passive', 'bounded_triage', 'full'",
                                   "'passive', 'unmentioned', 'bounded_triage', 'full'")
            if expanded != expected[name]:
                raise ValueError(f"Unexpected wake table layout: {name}")
        # Each select decodes through the declared table codec before any write.
        rows = {owner: owner.select(db) for owner in index.rows}
        saved = CandidateCheckpoint.current(db)
    with log._opened_wire_snapshot(opening=CurrentSnapshotOpening()) as snapshot:
        saved.require_source(snapshot, snapshot.metadata.root_id)
    result = {"rows": {owner.declared_name: len(values) for owner, values in rows.items()},
              "through": saved.byte_offset, "decode_failures": 0, "applied": False}
    if apply:
        with (log._opened_wire_snapshot(opening=CurrentSnapshotOpening()) as snapshot,
              _store_lock(index.path), closing(CoordinationStore.writer(index.path)) as db):
            db.execute("BEGIN IMMEDIATE")
            try:
                current = CandidateCheckpoint.current(db)
                current.require_source(snapshot, snapshot.metadata.root_id)
                if current.byte_offset > saved.byte_offset:
                    raise ValueError("Live wake index is newer than the saved rows")
                for owner in reversed(index.rows):
                    owner.drop(db)
                for owner, values in rows.items():
                    owner.create(db)
                    for value in values:
                        value.insert(db)
                index.require_schema(index._schema(db))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        result["applied"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("source", type=Path, help="Consistent backup of the old wake index")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(carry(args.root, args.source, apply=args.apply), sort_keys=True))
