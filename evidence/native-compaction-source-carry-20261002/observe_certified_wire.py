"""Bounded source inventory through installed WireLog and WireScan, no raw output.

Run only with the authentic original interpreter. This is an evidence query,
not a reader implementation, migration, input grant, or stopped-carry control.
The process's audit/SQLite authorizer refuses repair writes to the public root.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback

from agent_comms.checkpoint_seals import file_revision
from agent_comms.field_codec import FieldCodec
from agent_comms.input_origin import InputProvenance
from agent_comms.task_sources import NativeInputConstraintPin
from agent_comms.wire_log import WireLog
from agent_comms.wire_record import WireScan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    locks = {root / ".bus.jsonl.lock", root / ".bus_meta.json.lock"}
    if any(not path.is_file() for path in locks):
        raise ValueError("Existing canonical locks required; query creates no public resource")
    direct = json.loads(metadata.distribution("agent-comms").read_text("direct_url.json"))
    if direct["vcs_info"]["commit_id"] != "74877f2dd108ed211871a098064178eaf3a4fdb5":
        raise ValueError("Authentic reviewed Native5 interpreter required")
    # SQLite's external action taxonomy, scoped to this one observational process.
    write_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                     sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
                     sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
                     sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
                     sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
                     sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
                     sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
    denied = []
    constructing_connections = []
    attached_authorizers = []

    def authorize(action, first, second, database, context):
        if action in write_actions or (action == sqlite3.SQLITE_PRAGMA and second is not None
                                      and first.lower() not in {"synchronous", "query_only"}):
            denied.append("SQLite-write-or-mutating-pragma")
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def audit(event, arguments):
        if event == "sqlite3.connect/handle":
            # CPython emits this before Connection.__init__ has returned.
            # The existing reader next opens the bus stream after _connect's
            # connection-only synchronous pragma; attach before verification.
            constructing_connections.append(arguments[0])
        elif event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
            for connection in constructing_connections:
                connection.set_authorizer(authorize)
                attached_authorizers.append(True)
            constructing_connections.clear()
            path = Path(os.fsdecode(arguments[0])).absolute()
            if path.is_relative_to(root) and path not in locks and arguments[2] & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                denied.append("public-filesystem-write")
                raise PermissionError("Inventory cannot repair or change public files")
        elif event in {"os.rename", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.truncate"}:
            if any(isinstance(value, (str, bytes, os.PathLike))
                   and Path(os.fsdecode(value)).absolute().is_relative_to(root)
                   for value in arguments):
                denied.append("public-filesystem-mutation")
                raise PermissionError("Inventory cannot mutate public paths")

    sys.addaudithook(audit)
    started = time.monotonic()
    receipt = {"classification": "running-certified-source-inventory-not-stopped-carry",
               "observed_at": datetime.now(timezone.utc).isoformat(), "root": str(root),
               "installed_core": direct, "complete_at_recorded_prefix": False,
               "native_prompts": 0, "input_replays": 0, "owner_signals": 0}
    counts = Counter()
    pins = []
    try:
        bus = WireLog(root / "bus.jsonl")
        # NONBLOCK refuses contention rather than waiting on a user turn.
        with bus.certified_read(blocking=False) as source:
            if source.witness.offset > 8 * 1024 * 1024:
                raise ValueError("Original prefix exceeds this inventory's 8 MiB bound")
            before = file_revision(bus.path.stat())
            certificate_before = file_revision((root / "private_bus_checkpoint.sqlite3").stat())
            marker_before = file_revision(bus.metadata_path.stat())
            receipt["certificate"] = FieldCodec.encode(source.witness)
            scan = WireScan(source.marker)
            digest = hashlib.sha256()
            source.stream.seek(0)
            while source.stream.tell() < source.witness.offset:
                if counts["wire_records"] >= 10000 or time.monotonic() - started > 3:
                    raise TimeoutError("Bounded certified inventory exhausted its source budget")
                offset = source.stream.tell()
                raw = source.stream.readline(scan.max_row_bytes + 1)
                digest.update(raw)
                record = scan.read(raw)
                counts["wire_records"] += 1
                for message in record.messages():
                    counts["messages"] += 1
                    if isinstance(message.task, NativeInputConstraintPin):
                        counts["native_input_constraint_pins"] += 1
                        encoded = FieldCodec.encode(message.task.subject)
                        pins.append({"seq": message.seq, "message_id": message.message_id,
                                     "offset": offset, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                                     "subject_type": type(message.task.subject).__name__,
                                     "subject_fields": sorted(encoded),
                                     "digest_present": "digest" in encoded})
                for manifest in record.context_manifests():
                    counts["context_manifests"] += 1
                    segments = list(manifest.segments)
                    while segments:
                        segment = segments.pop()
                        segments.extend(segment.contributors)
                        for provenance in segment.provenance:
                            if isinstance(provenance, InputProvenance):
                                counts["context_input_provenances"] += 1
                                counts["context_input_provenances_with_digest"] += (
                                    "digest" in FieldCodec.encode(provenance))
            if source.stream.tell() != source.witness.offset:
                raise ValueError("Scan did not end at the exact certified prefix")
            source.require_current()
            if before != file_revision(bus.path.stat()) or certificate_before != file_revision(
                (root / "private_bus_checkpoint.sqlite3").stat()
            ) or marker_before != file_revision(bus.metadata_path.stat()):
                raise ValueError("Original certified resources changed during observation")
            receipt.update(complete_at_recorded_prefix=True, prefix_sha256=digest.hexdigest(),
                           final_sequence=scan.previous_sequence,
                           source_resources_unchanged_under_certificate=True)
    except Exception as error:
        # Only the exception's class is exposed; payload-bearing text stays private.
        receipt["error_type"] = type(error).__name__
        if error.__cause__ is not None:
            receipt["cause_type"] = type(error.__cause__).__name__
            if isinstance(error.__cause__, sqlite3.Error):
                receipt["sqlite_refusal"] = str(error.__cause__)[:200]
        receipt["error_frames"] = [{"file": frame.filename, "line": frame.lineno,
                                     "function": frame.name}
                                    for frame in traceback.extract_tb(error.__traceback__)]
        if type(error).__name__ == "RelationViolationError":
            receipt["source_refusal"] = str(error)[:300]
    receipt.update(counts=dict(counts), pins=pins, denied_writes=denied,
                   sqlite_authorizers_attached=len(attached_authorizers),
                   duration_seconds=round(time.monotonic() - started, 6))
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: receipt[key] for key in (
        "complete_at_recorded_prefix", "counts", "duration_seconds", "denied_writes")}))
    if not receipt["complete_at_recorded_prefix"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
