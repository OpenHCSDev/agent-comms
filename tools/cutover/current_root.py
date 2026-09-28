"""Rehearse one complete current root with retained UNKNOWN evidence; never activate."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from channel_catalog import read_source
from registry_history import stage as stage_registry
from wire_history import stage as stage_wire

from agent_comms.catalog_store import ChannelCatalog
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.store_files import _atomic_write_text, file_revision
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.wire_log import WireLog
from agent_comms.wire_metadata import WritableAccess


@dataclass(frozen=True)
class RootRehearsal:
    source: str
    candidate: str
    messages: int
    admission_after_seq: int
    threads: int
    unresolved_inputs: int
    archived_runtime_databases: tuple[str, ...]


def stage(source: Path, destination: Path) -> RootRehearsal:
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
    )
    before = tuple(file_revision(source / name) for name in durable)
    wire_receipt = stage_wire(source, destination, WritableAccess())
    retained = destination / "precutover-evidence"
    retained.mkdir(mode=0o700)
    registry_receipt = stage_registry(source, retained / "registry")
    for name in ("registry.json", "goal_history.sqlite3", "goal_pause_events.json"):
        path = retained / "registry" / name
        if path.exists():
            shutil.copy2(path, destination / name)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(FieldCodec.encode(stage(args.source, args.destination)), indent=2))
