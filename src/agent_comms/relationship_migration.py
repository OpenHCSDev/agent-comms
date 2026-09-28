"""Explicit one-way conversion of saved relationship v1 data to the current schema.

The runtime never imports this converter. Preview is read-only; deployment may
apply it while holding the existing wire/document locks. No old-client reader
or writer is retained in RelationshipStore.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Literal

from .field_codec import FieldCodec
from .registration import Registration
from .registry_document import RegistrySnapshot
from .registry_store import RegistryStore
from .relationships import (
    Collaboration,
    CollaborationRevision,
    RelationshipDocument,
    RelationshipOrder,
    RelationshipStore,
)
from .store_files import _store_lock


@dataclass(frozen=True, slots=True)
class VersionOneRelationships:
    version: Literal[1]
    collaborations: tuple[CollaborationRevision, ...]
    orders: tuple[RelationshipOrder, ...]


def convert_relationships(payload: object, registry: RegistrySnapshot) -> RelationshipDocument:
    """Retain every original duplicate record, including orientation and timestamps."""
    old = FieldCodec.decode(VersionOneRelationships, payload)
    groups: dict[frozenset[tuple[str, float]], list[CollaborationRevision]] = {}
    for record in old.collaborations:
        groups.setdefault(record.resolved(registry).pair_identity, []).append(record)
    edges = []
    for originals in groups.values():
        newest = max(originals, key=lambda row: (row.updated_at, row.owner, row.peer))
        resolved = newest.resolved(registry)
        notes = tuple(dict.fromkeys(row.note for row in originals if row.note))
        edge = Collaboration(
            **{
                field.name: getattr(resolved, field.name) for field in fields(CollaborationRevision)
            },
            history=tuple(originals) if len(originals) > 1 or newest != resolved else (),
        )
        edges.append(
            replace(
                edge, note="\n".join(notes), created_at=min(row.created_at for row in originals)
            )
        )
    # Duplicate sort identities are rejected, never silently overwritten.
    return RelationshipDocument(tuple(edges), old.orders).resolved(registry)


def migrate_relationships(path: Path, registry: RegistrySnapshot) -> RelationshipDocument:
    """Called under the wire lock; atomic A8 publication leaves old bytes on failure."""
    store = RelationshipStore(path)
    with store.locked():
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return store.empty()  # Do not materialize an absent optional document.
        if isinstance(payload, dict) and payload.get("version") == 2:
            return FieldCodec.decode(RelationshipDocument, payload)
        converted = convert_relationships(payload, registry)
        store._write_unlocked(
            json.dumps(FieldCodec.encode(converted), indent=store.json_indent) + store.json_suffix
        )
        return converted


def preview(root: Path) -> RelationshipDocument:
    """Inspect saved bytes without creating even a lock file in the source root."""
    registry_store = RegistryStore(root / "registry.json")
    registry = registry_store._decode(json.loads(registry_store.path.read_text())).snapshot()
    path = root / RelationshipStore.filename
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return RelationshipDocument()
    if isinstance(payload, dict) and payload.get("version") == 2:
        return FieldCodec.decode(RelationshipDocument, payload)
    return convert_relationships(payload, registry)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Publish the one-way migration")
    args = parser.parse_args(argv)
    if args.apply:
        with _store_lock(args.root / "wire"):
            registry = Registration(args.root / "registry.json").snapshot()
            document = migrate_relationships(args.root / RelationshipStore.filename, registry)
    else:
        document = preview(args.root)
    print(
        json.dumps(
            {
                "applied": args.apply,
                "schema": document.version,
                "collaborations": len(document.collaborations),
                "orders": len(document.orders),
                "historical_records": sum(len(edge.history) for edge in document.collaborations),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
