"""One-shot v2 checkpoint preparation before activating the v3 route reader.

No runtime legacy reader: reuse the unchanged owned row data, build the current
indexes from their declarations, and publish only the completed database. The
original bus, v2 index and installed runtime are never modified.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from contextlib import closing
from pathlib import Path

from agent_comms.bus_route_counts import BusRouteCounts, RouteSourceRow, RouteTable
from agent_comms.coordination_database import CoordinationStore
from agent_comms.typed_table import TypedTable


def prepare(source: Path, bus: Path) -> dict:
    destination = BusRouteCounts(bus).path
    if destination.exists():
        raise FileExistsError(f"Destination already exists; inspect, do not replay: {destination}")
    if source.resolve() == destination.resolve():
        raise ValueError("Preparation must not modify its source checkpoint")
    tables = TypedTable.members_with(RouteTable)
    with tempfile.TemporaryDirectory(prefix=".route-prefix-", dir=destination.parent) as scratch:
        staged = Path(scratch) / destination.name
        with closing(CoordinationStore.writer(staged, uri=True)) as db, bus.open("rb") as original:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("ATTACH DATABASE ? AS checkpoint", (source.resolve().as_uri()+"?mode=ro",))
            db.execute("BEGIN")
            columns = RouteSourceRow._column_list(RouteSourceRow.columns())
            records = RouteSourceRow.read(db.execute(
                f'SELECT {columns} FROM checkpoint."{RouteSourceRow.declared_name}"'
            ))
            if len(records) != 1 or not records[0].matches_file(original):
                raise ValueError("Checkpoint does not cover the original bus descriptor")
            counts = {}
            for owner in tables:
                owner.create(db)
            for owner in tables:
                columns = owner._column_list(owner.columns())
                name = owner.declared_name
                db.execute(f'INSERT INTO main."{name}" ({columns}) '
                           f'SELECT {columns} FROM checkpoint."{name}"')
                counts[name] = db.execute(f'SELECT count(*) FROM main."{name}"').fetchone()[0]
            if (not records[0].matches_file(original)
                    or db.execute("PRAGMA foreign_key_check").fetchall()):
                raise ValueError("Prepared checkpoint lost its source or reference integrity")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # link, rather than replace: a concurrent preparation cannot be overwritten.
        os.link(staged, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return {"source": str(source), "destination": str(destination), "rows": counts,
            "bytes_covered": records[0].revision.size if records[0].revision else 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--bus", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.bus)))


if __name__ == "__main__":
    main()
