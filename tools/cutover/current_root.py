"""Rehearse one complete current root with retained UNKNOWN evidence; never activate."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, fields, replace
from pathlib import Path

from channel_catalog import read_source
from registry_history import stage as stage_registry
from transcript_annotations import stage as stage_annotations
from wire_history import stage as stage_wire

from agent_comms.catalog_store import ChannelCatalog
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.store_files import _atomic_write_text, file_revision
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.wire_log import WireLog
from agent_comms.wire_metadata import ArchivedAccess, WireAccess, WritableAccess


@dataclass(frozen=True)
class StoredReadDocument(ReadDocument):
    """Retired importer flag, accepted only at this one-shot boundary."""

    migrated: bool = False

    def current(self, source_bus: Path, destination_bus: Path) -> ReadDocument:
        current = ReadDocument(
            **{item.name: getattr(self, item.name) for item in fields(ReadDocument)}
        )
        # Bus IDs and registry incarnations were preserved by the staged rewrite.
        # Rebind only evidence belonging to this source, never stale old evidence.
        if self.bus_identity == ReadLedger.bus_identity(source_bus):
            current = replace(current, bus_identity=ReadLedger.bus_identity(destination_bus))
        return current


@dataclass(frozen=True)
class RootRehearsal:
    source: str
    candidate: str
    messages: int
    admission_after_seq: int
    threads: int
    unresolved_inputs: int
    archived_runtime_databases: tuple[str, ...]


def stage(source: Path, destination: Path, access: WireAccess = WritableAccess()) -> RootRehearsal:
    """This stages the active root, not attached history or final install state.

    Runtime journals are preserved as evidence, excluded from fresh runtime
    authority. Input dispositions remain current typed observations. Nothing
    copies a process binding, schedules an old input, or contacts a provider.
    """
    source, destination = source.resolve(), destination.absolute()
    durable = (
        "registry.json",
        "goal_history.sqlite3",
        "goal_pause_events.json",
        "bus.jsonl",
        "bus_meta.json",
        ChannelCatalog.filename,
        InputDispositions.filename,
        ReadLedger.filename,
        "transcript_routes.sqlite3",
        "transcript_routes.json",
    )
    before = tuple(file_revision(source / name) for name in durable)
    wire_receipt = stage_wire(source, destination, access)
    retained = destination / "precutover-evidence"
    retained.mkdir(mode=0o700)
    registry_receipt = stage_registry(source, retained / "registry")
    for name in ("registry.json", "goal_history.sqlite3", "goal_pause_events.json"):
        path = retained / "registry" / name
        if path.exists():
            shutil.copy2(path, destination / name)
    stage_annotations(source, retained / "annotations")
    shutil.copy2(
        retained / "annotations" / "transcript_routes.sqlite3",
        destination / "transcript_routes.sqlite3",
    )
    read_path = source / ReadLedger.filename
    if read_path.exists():
        original_reads = read_path.read_text()
        reads = FieldCodec.decode(StoredReadDocument, json.loads(original_reads)).current(
            source / "bus.jsonl", destination / "bus.jsonl"
        )
        _atomic_write_text(retained / "original-read-ledger.json", original_reads)
        _atomic_write_text(destination / ReadLedger.filename, json.dumps(FieldCodec.encode(reads)))
    _atomic_write_text(
        destination / ChannelCatalog.filename,
        json.dumps(FieldCodec.encode(read_source(source))),
    )
    input_path = source / InputDispositions.filename
    inputs = (
        FieldCodec.decode(InputDocument, json.loads(input_path.read_text()))
        if input_path.exists()
        else InputDocument()
    )
    _atomic_write_text(
        destination / InputDispositions.filename, json.dumps(FieldCodec.encode(inputs))
    )
    # Old coordinator and compaction authority must not be opened by new code.
    # Keep complete SQLite snapshots, including UNKNOWN/replay audit records.
    archived = []
    for name in (
        "coordination.sqlite3",
        "native_prompt_bindings.sqlite3",
        "compaction-commits.sqlite3",
        "goal_attempts.sqlite3",
    ):
        path = source / name
        if not path.exists():
            continue
        saved = retained / name
        with (
            closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as old,
            closing(sqlite3.connect(saved)) as copy,
        ):
            old.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError(f"Invalid retained runtime evidence: {name}")
        os.chmod(saved, 0o600)
        archived.append(name)
    log = WireLog(destination / "bus.jsonl")
    marker = log.read_metadata_unlocked(required=True)
    # The candidate is unpublished. Establish its registry guard before making
    # the completed marker visible to a current reader.
    (destination / "bus_meta.json").unlink()
    guard = PrivateRegistryGuard(destination / "registry.json", marker.root_id)
    guard.create_pending()
    log.write_metadata_unlocked(marker)
    guard.commit_initial()
    install_private_bus_checkpoint(log)
    reopened = Comms(destination)
    snapshot = reopened.registry.snapshot()
    WakeCandidateIndex(reopened.bus).maintain(rebuild=True)
    observed = InputDispositions(destination / InputDispositions.filename).read()
    if observed != inputs or len(snapshot.threads) != registry_receipt.threads:
        raise ValueError("Current root differs from retained identities or input outcomes")
    if before != tuple(file_revision(source / name) for name in durable):
        raise ValueError("Source changed during rehearsal; candidate is not installable")
    receipt = RootRehearsal(
        str(source),
        str(destination),
        wire_receipt.messages,
        wire_receipt.admission_after_seq,
        len(snapshot.threads),
        sum(row.unresolved for row in observed.rows.values()),
        tuple(archived),
    )
    _atomic_write_text(
        destination / "rehearsal.json", json.dumps(FieldCodec.encode(receipt), indent=2)
    )
    return receipt


def stage_attached_history(source: Path, destination: Path) -> tuple[RootRehearsal, ...]:
    """Rewrite already attached snapshots, retaining their original provenance."""
    from agent_comms.historical_views import HistorySource

    manifest = source / "history_sources.json"
    before = file_revision(manifest)
    original = manifest.read_text() if manifest.exists() else "[]"
    retained = FieldCodec.decode(tuple[HistorySource, ...], json.loads(original))
    history = destination / "history"
    history.mkdir(mode=0o700, exist_ok=False)
    current, receipts = [], []
    for index, item in enumerate(retained):
        item.validate()
        target = history / f"source-{index}"
        receipts.append(stage(Path(item.root), target, ArchivedAccess()))
        item.validate()
        marker = WireLog(target / "bus.jsonl").read_metadata_unlocked(required=True)
        current.append(
            replace(
                item,
                root=str(target.resolve()),
                wire_root_id=marker.root_id,
                snapshot_bus_revision=file_revision(target / "bus.jsonl"),
                snapshot_registry_revision=file_revision(target / "registry.json"),
            )
        )
    if file_revision(manifest) != before:
        raise ValueError("Attached history changed during staging; candidate is not installable")
    _atomic_write_text(destination / "precutover-evidence/original-history-sources.json", original)
    _atomic_write_text(destination / manifest.name, json.dumps(FieldCodec.encode(tuple(current))))
    return tuple(receipts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(FieldCodec.encode(stage(args.source, args.destination)), indent=2))
