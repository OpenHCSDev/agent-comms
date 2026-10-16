"""One-shot COPY dry-run for unstamped route metadata; never opens a live writer.

This does not repair canonical unstamped wire records. Copy results cannot clear
installation: rebuilding from those originals loses the inferred index stamps.
A live/canonical operation needs an explicit reviewed disposition, not --apply.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.bus_route_counts import (
    LeafRouteTimeNode, RouteEntryRow, RouteIdentityRow, RouteSourceRow,
    RouteTable, RouteTimeRow,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import FileRevision
from agent_comms.registration import Registration
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.typed_table import In, TypedStore, TypedTable


@dataclass(frozen=True)
class CopiedRouteStore(TypedStore):
    root: Path
    filename: ClassVar[str] = 'bus_route_counts.v3.sqlite3'
    rows: ClassVar = tuple(TypedTable.members_with(RouteTable))


@dataclass(frozen=True)
class ResolvedRoute:
    original: RouteIdentityRow
    incarnation: ThreadIncarnation

@dataclass(frozen=True)
class CarryResult:
    copied_index: str
    registry_revision: FileRevision
    stamped: int
    unresolvable: tuple[RouteIdentityRow, ...]
    merged_keys: int
    changed_entries: int
    durability: str


def dry_run_copy(source_root: Path, output: Path) -> CarryResult:
    """Capture a fresh SQLite backup; modify only that explicitly new copy."""
    output.mkdir(mode=0o700, parents=True)
    store = CopiedRouteStore(output)
    source_path = source_root / store.filename
    if source_path.resolve() == store.path.resolve():
        raise ValueError('Copy destination cannot be the source index')
    with Registration(source_root / 'registry.json').store.reading() as document:
        snapshot = document.snapshot()
        revision = FileRevision.from_stat((source_root / 'registry.json').stat())
    source = CoordinationStore.read_snapshot(source_path)
    destination = CoordinationStore.writer(store.path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    store.path.chmod(0o600)

    resolved, missing = [], []
    with store.snapshot() as connection:
        coverage = RouteSourceRow.select(connection)
        originals = RouteIdentityRow.select(connection, sender_lookup='')
        for row in originals:
            try:
                incarnation = snapshot.participant(row.sender).incarnation
            except UnregisteredThreadError:
                missing.append(row)
                continue
            if incarnation.has_recorded_birth:
                resolved.append(ResolvedRoute(row, incarnation))
            else:
                missing.append(row)
    # Retain the recorded birth evidence before the copy mutation can fail.
    (output / 'plan.json').write_text(json.dumps(FieldCodec.encode(tuple(resolved)), indent=2) + '\n')
    merged = changed = 0
    with store.writing() as connection:
        for planned in resolved:
            original = planned.original
            lookup = stable_thread_lookup(planned.incarnation.created_at)
            entries = RouteEntryRow.select(
                connection, target=original.target, sender=original.sender,
                sender_lookup=In(('', lookup)), order_by=('seq',),
            )
            if not entries:
                raise ValueError(f'Route {original.first_seq} has no recorded entries')
            merged += bool(RouteIdentityRow.select(
                connection, target=original.target, sender=original.sender, sender_lookup=lookup,
            ))
            # Prefix roots and equal-time predecessor chains must be rebuilt
            # together when the old and stamped keys collide. The existing
            # timestamp-node owner performs every rank/tree mutation.
            first, *remaining = entries
            root = RouteTimeRow.store(connection, LeafRouteTimeNode(
                count=1, timestamp=first.timestamp, head_seq=first.seq,
            ))
            RouteEntryRow.update(connection, where='seq=?', parameters=(first.seq,),
                                 sender_lookup=lookup, time_root=root.id,
                                 previous_at_timestamp=None)
            for entry in remaining:
                root, previous = root.node.add(connection, root, LeafRouteTimeNode(
                    count=1, timestamp=entry.timestamp, head_seq=entry.seq,
                ))
                RouteEntryRow.update(connection, where='seq=?', parameters=(entry.seq,),
                                     sender_lookup=lookup, time_root=root.id,
                                     previous_at_timestamp=previous)
            if root.node.count != len(entries):
                raise ValueError('Merged route lost timestamp membership')
            RouteIdentityRow.delete(connection, target=original.target, sender=original.sender,
                                    sender_lookup=In(('', lookup)))
            RouteIdentityRow(original.target, original.sender, lookup, first.seq).insert(connection)
            changed += sum(entry.sender_lookup == '' for entry in entries)
        if RouteSourceRow.select(connection) != coverage:
            raise ValueError('Carry changed the original source cut')
        if frozenset(RouteIdentityRow.select(connection, sender_lookup='')) != frozenset(missing):
            raise ValueError('Unresolved routes changed during carry')
    result = CarryResult(
        str(store.path), revision, len(resolved), tuple(missing), merged, changed,
        'Projection-only: canonical rebuild recreates unstamped keys; not install acceptance',
    )
    (output / 'result.json').write_text(json.dumps(FieldCodec.encode(result), indent=2) + '\n')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_root', type=Path)
    parser.add_argument('new_copy', type=Path)
    args = parser.parse_args()
    print(json.dumps(FieldCodec.encode(dry_run_copy(args.source_root, args.new_copy))))


if __name__ == '__main__':
    main()
