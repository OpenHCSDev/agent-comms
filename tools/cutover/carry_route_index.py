"""Route index v5 into v6 without reading bus history.

Only the saved route source row changes shape; every route row is copied as is.
A bus without a v5 index, or one that already has its v6 index, has nothing to carry.
The saved rows belong to one bus file: a copy or restore of the bus leaves the
v6 index for its reader to build.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import closing
from pathlib import Path

from agent_comms.bus_route_counts import BusRouteCounts, RecordedRouteSourceRow, RouteTable
from agent_comms.coordination_database import CoordinationStore
from agent_comms.store_files import _store_lock
from agent_comms.typed_table import TypedTable
from agent_comms.wire_log import CurrentSnapshotOpening, WireLog

from cutover.carry import Carried, Carry


class RouteIndexV6(Carry):
    """The v5 route index's rows in the v6 declaration."""

    after = "carry_external_pages"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        old = root / "bus_route_counts.v5.sqlite3"
        index = BusRouteCounts(root / "bus.jsonl")
        if not old.exists() or index.path.exists():
            return Carried(0)
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
            recorded = [] if source_rows == [(None, None)] else RecordedRouteSourceRow.select(source)
            counts = {owner.declared_name: sum(1 for _ in owner.stream(source))
                      for owner in owners if owner is not RecordedRouteSourceRow}
            if not recorded and any(counts.values()):
                raise ValueError("Route rows exist without a saved source")
            with WireLog(index.bus_path)._opened_wire_snapshot(opening=CurrentSnapshotOpening()) as cut:
                if not all(row.matches_source(cut) for row in recorded):
                    # Rows saved for another bus file (a copy, a restore) are not this
                    # bus's; its reader builds the v6 index from history instead.
                    return Carried(0, {"rebuilt_by_reader": True})
            if apply:
                with _store_lock(index.path):
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
        rewritten = tuple(path.name for path in root.glob(f"{index.path.name}*"))
        rewritten += tuple(path.name for path in root.glob(f"{old.name}-*"))
        return Carried(1, {**counts, "source_rows": len(recorded)}, rewritten if apply else ())


if __name__ == "__main__":
    RouteIndexV6.main()
