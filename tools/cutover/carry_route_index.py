"""Stopped-writer carry of route index v5 into v6 without reading bus history."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from contextlib import closing
from pathlib import Path

from agent_comms.bus_route_counts import BusRouteCounts, RecordedRouteSourceRow, RouteTable
from agent_comms.coordination_database import CoordinationStore
from agent_comms.store_files import _store_lock
from agent_comms.typed_table import TypedTable
from agent_comms.wire_log import CurrentSnapshotOpening, WireLog


def carry(root: Path, *, apply: bool = False) -> dict:
    old = root / "bus_route_counts.v5.sqlite3"
    index = BusRouteCounts(root / "bus.jsonl")
    owners = tuple(TypedTable.members_with(RouteTable))
    with closing(CoordinationStore.read_snapshot(old)) as source:
        expected = {name: sql for owner in owners for name, sql in owner.schema_objects().items()}
        actual = dict(source.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))
        if actual.keys() != expected.keys() or any(
                actual[name] != sql for name, sql in expected.items()
                if name != RecordedRouteSourceRow.declared_name):
            raise ValueError("Unexpected route index layout")
        source_rows = source.execute('SELECT revision,tail_digest FROM route_source').fetchall()
        if source_rows == [(None, None)]:
            recorded = []
        else:
            recorded = RecordedRouteSourceRow.select(source)
        counts = {owner.declared_name: sum(1 for _ in owner.stream(source))
                  for owner in owners if owner is not RecordedRouteSourceRow}
        if not recorded and any(counts.values()):
            raise ValueError("Route rows exist without a saved source")
        with WireLog(index.bus_path)._opened_wire_snapshot(opening=CurrentSnapshotOpening()) as cut:
            for row in recorded:
                if not row.matches_source(cut):
                    raise ValueError("Saved route index does not match this bus")
        report = {"rows": counts, "source_rows": len(recorded),
                  "decode_failures": 0, "applied": False}
        if apply:
            with _store_lock(index.path):
                if index.path.exists():
                    raise FileExistsError("Refusing to overwrite an existing v6 route index")
                descriptor, temporary = tempfile.mkstemp(prefix="route-carry-", suffix=".sqlite3", dir=root)
                os.close(descriptor)
                with closing(CoordinationStore.writer(Path(temporary))) as destination:
                    source.backup(destination)
                    destination.execute("BEGIN IMMEDIATE")
                    try:
                        RecordedRouteSourceRow.drop(destination)
                        RecordedRouteSourceRow.create(destination)
                        for row in recorded:
                            row.insert(destination)
                        destination.commit()
                    except BaseException:
                        destination.rollback()
                        raise
                os.replace(temporary, index.path)
            report["applied"] = True
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(carry(args.root, apply=args.apply), sort_keys=True))
