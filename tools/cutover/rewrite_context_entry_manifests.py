#!/usr/bin/env python3
"""One-time hard cutover from inline context manifests to entry references."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.turn_context import ContextManifest, RecordedContextManifest
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.wire_log import WireLog
from agent_comms.wire_record import (
    ContextEntryManifestWireObservation,
    ObservationWireRecord,
    RecordedContextManifestWireObservation,
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while raw := source.read(8 * 1024 * 1024):
            value.update(raw)
    return value.hexdigest()


def plan(root: Path, output: Path, receipt: Path) -> None:
    source = root / "bus.jsonl"
    if output.exists() or receipt.exists():
        raise ValueError("cutover output already exists")
    stat = source.stat()
    entries_db = output.with_suffix(".entries.sqlite3")
    messages = last_seq = old_contexts = entry_rows = turn_rows = 0
    source_hash = hashlib.sha256(); output_hash = hashlib.sha256()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as target, sqlite3.connect(entries_db) as known, source.open("rb") as rows:
            known.execute("CREATE TABLE entry(session_file TEXT,entry_id TEXT,PRIMARY KEY(session_file,entry_id)) WITHOUT ROWID")

            def write(value: dict) -> None:
                raw = json.dumps(value, allow_nan=False).encode() + b"\n"
                target.write(raw); output_hash.update(raw)

            for raw in rows:
                source_hash.update(raw)
                value = json.loads(raw)
                observation = value.get("observation") if isinstance(value, dict) else None
                if not isinstance(observation, dict) or observation.get("kind") != "context_manifest":
                    if isinstance(value, dict) and type(value.get("seq")) is int:
                        messages += 1
                        if value["seq"] <= last_seq:
                            raise ValueError("message sequence is not increasing")
                        last_seq = value["seq"]
                    target.write(raw); output_hash.update(raw)
                    continue
                old_contexts += 1
                manifest = FieldCodec.decode(ContextManifest, observation["manifest"])
                recorded, entries = RecordedContextManifest.capture(manifest)
                fresh = []
                for entry in entries:
                    reference = entry.reference
                    key = (reference.identity.session_file, reference.entry_id)
                    if known.execute(
                        "SELECT 1 FROM entry WHERE session_file=? AND entry_id=?", key
                    ).fetchone() is None:
                        known.execute("INSERT INTO entry VALUES (?,?)", key)
                        fresh.append(entry)
                if fresh:
                    write(FieldCodec.encode(ObservationWireRecord(
                        ContextEntryManifestWireObservation(tuple(fresh)))))
                    entry_rows += 1
                write(FieldCodec.encode(ObservationWireRecord(
                    RecordedContextManifestWireObservation(recorded))))
                turn_rows += 1
            target.flush(); os.fsync(target.fileno())
    finally:
        entries_db.unlink(missing_ok=True)
    result = {
        "root": str(root), "source": str(source), "output": str(output),
        "source_revision": [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns],
        "source_sha256": source_hash.hexdigest(), "output_sha256": output_hash.hexdigest(),
        "source_bytes": stat.st_size, "output_bytes": output.stat().st_size,
        "message_rows": messages, "last_seq": last_seq, "old_context_rows": old_contexts,
        "entry_rows": entry_rows, "turn_rows": turn_rows,
    }
    receipt.write_text(json.dumps(result, indent=2) + "\n")


def apply(root: Path, output: Path, receipt: Path) -> None:
    expected = json.loads(receipt.read_text())
    source = root / "bus.jsonl"
    stat = source.stat()
    revision = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    if revision != expected["source_revision"] or digest(source) != expected["source_sha256"]:
        raise ValueError("source bus drifted after cutover plan")
    if output.stat().st_size != expected["output_bytes"] or digest(output) != expected["output_sha256"]:
        raise ValueError("planned compact bus changed")
    service = Comms(root) if expected["old_context_rows"] else None
    log = service.bus.log if service is not None else WireLog(source)
    backup = root / "bus.pre-context-entry.jsonl"
    checkpoint = root / "private_bus_checkpoint.sqlite3"
    checkpoint_backup = root / "private_bus_checkpoint.pre-context-entry.sqlite3"
    if backup.exists() or checkpoint_backup.exists():
        raise ValueError("cutover backup already exists")
    from agent_comms.child_process import Platform
    from agent_comms.store_files import _store_lock_file

    platform = Platform.current()
    with _store_lock_file(source) as lock_file:
        platform.acquire_store_lock(lock_file.fileno(), shared=False, blocking=True)
        try:
            marker = log._private_marker_unlocked()
            if expected["old_context_rows"]:
                os.replace(source, backup)
                os.replace(output, source)
            else:
                # Message-only retained archives need the new checkpoint schema,
                # but their immutable bus inode/revision must not change.
                shutil.copy2(source, backup)
                output.unlink()
            if checkpoint.exists():
                os.replace(checkpoint, checkpoint_backup)
            marker.last_seq = expected["last_seq"]
            marker.checkpoint_version = None
            marker.checkpoint_seal = None
            log.write_metadata_unlocked(marker)
            install_private_bus_checkpoint(log, _bus_locked=True)
        finally:
            platform.release_store_lock(lock_file.fileno())
    for path in (
        root / "bus_page_index.sqlite3",
        root / "wake_candidates.sqlite3",
        root / "bus_activity_latest.json",
        *root.glob("bus_display_*.json"),
    ):
        path.unlink(missing_ok=True)
    from agent_comms.bus_page_index import BusPageIndex
    with BusPageIndex(source) as index:
        if not index.sync():
            raise ValueError("bus page index did not reach compact source tail")
    if service is not None:
        candidate = WakeCandidateIndex(service.bus)
        complete = candidate.maintain(rebuild=True, max_rows=256, max_bytes=8 * 1024 * 1024)
        while not complete:
            complete = candidate.maintain(max_rows=256, max_bytes=8 * 1024 * 1024)
    if len(log.full_history()) != expected["message_rows"]:
        raise ValueError("message row count changed during cutover")
    expected["applied"] = True
    expected["backup"] = str(backup)
    receipt.write_text(json.dumps(expected, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    (plan if args.mode == "plan" else apply)(
        args.root.absolute(), args.output.absolute(), args.receipt.absolute()
    )


if __name__ == "__main__":
    main()
