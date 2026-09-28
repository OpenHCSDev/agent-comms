"""Exercise the actual D22 directory exchange on a disposable retained-root copy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from current_root import stage, stage_attached_history
from install_root import assert_quiet, ignore_sockets, install

from agent_comms.active_route import read_active_route
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.store_files import file_revision

PROJECT = Path(__file__).resolve().parents[2]


def file_contents(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".lock"):
            with path.open("rb") as stream:
                result[str(path.relative_to(root))] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


live = read_active_route().root
try:
    assert_quiet(live)
except ValueError as error:
    print(f"PASS live installation refused while clients remain: {error}", flush=True)
else:
    raise AssertionError("Expected the live clients to prevent installation")

with tempfile.TemporaryDirectory(prefix="d22-install-", dir=PROJECT / ".artifacts") as directory:
    owned = Path(directory)
    source, candidate, backup = (owned / name for name in ("source", "candidate", "backup"))
    shutil.copytree(live, source, symlinks=True, ignore=ignore_sockets)
    # SQLite backup captures complete WAL contents from each actual database.
    for original in live.rglob("*.sqlite3"):
        copied = source / original.relative_to(live)
        copied.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(copied) + suffix).unlink(missing_ok=True)
        with (
            closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(copied)) as writer,
        ):
            reader.backup(writer)
        os.chmod(copied, 0o600)
    # Detach process bindings only in the copy. Retain the original live bytes.
    registry = source / "registry.json"
    raw = json.loads(registry.read_text())
    for row in raw["threads"].values():
        row["pid"] = 0
        row["active_turn"] = None
    payload = json.dumps(raw).encode()
    guard = PrivateRegistryGuard(
        registry, json.loads((source / "bus_meta.json").read_text())["wire_root_id"]
    )
    digest = hashlib.sha256(b"present\0" + payload).digest()
    seq, index = guard.prepare(digest)
    registry.write_bytes(payload)
    guard.commit(seq, index, digest)
    history = FieldCodec.decode(
        tuple[HistorySource, ...], json.loads((source / "history_sources.json").read_text())
    )
    copied_history = tuple(
        replace(
            item,
            root=str(source / Path(item.root).relative_to(live)),
            snapshot_bus_revision=file_revision(
                source / Path(item.root).relative_to(live) / "bus.jsonl"
            ),
            snapshot_registry_revision=file_revision(
                source / Path(item.root).relative_to(live) / "registry.json"
            ),
        )
        for item in history
    )
    (source / "history_sources.json").write_text(json.dumps(FieldCodec.encode(copied_history)))
    # Keep the live human read memberships meaningful in the copied inode domain.
    reads_path = source / ReadLedger.filename
    reads_raw = json.loads(reads_path.read_text())
    if reads_raw["bus_identity"] == list(ReadLedger.bus_identity(live / "bus.jsonl")):
        reads_raw["bus_identity"] = list(ReadLedger.bus_identity(source / "bus.jsonl"))
    internal_session = next((source / "native-sessions").rglob("*.jsonl"))
    original_read_key = ReadLedger._transcript_key(
        "migration-test", str(internal_session), internal_session.stat().st_ino
    )
    reads_raw["transcripts"][original_read_key] = 1
    reads_path.write_text(json.dumps(reads_raw))
    before = file_contents(source)
    expected = stage(source, candidate)
    archives = stage_attached_history(source, candidate)
    assert file_contents(source) == before, "Staging mutated its source"
    old_inode = source.stat().st_ino
    actual = install(source, candidate, backup)
    assert actual == expected
    assert backup.stat().st_ino == old_inode, "Original directory was not retained atomically"
    assert source.stat().st_ino != old_inode
    assert file_contents(backup) == before, "Original backup content changed"
    current = Comms(source)
    archive_sources = FieldCodec.decode(
        tuple[HistorySource, ...], json.loads((source / "history_sources.json").read_text())
    )
    for item in archive_sources:
        item.validate()
        item.registry().all_threads()
    installed_reads = FieldCodec.decode(ReadDocument, json.loads(reads_path.read_text()))
    staged_reads = FieldCodec.decode(
        ReadDocument, json.loads((candidate / ReadLedger.filename).read_text())
    )
    expected_transcripts = dict(staged_reads.transcripts)
    expected_transcripts.pop(original_read_key)
    installed_read_key = ReadLedger._transcript_key(
        "migration-test", str(internal_session), internal_session.stat().st_ino
    )
    expected_transcripts[installed_read_key] = 1
    assert original_read_key != installed_read_key
    assert installed_reads == replace(
        staged_reads,
        bus_identity=ReadLedger.bus_identity(source / "bus.jsonl"),
        transcripts=expected_transcripts,
    )
    installed_contents = file_contents(source)
    for name, digest in before.items():
        if name.startswith(("native-sessions/", "diagnostics/")):
            assert installed_contents[name] == digest, name
    print(
        json.dumps(
            {
                "messages": actual.messages,
                "archived_messages": sum(item.messages for item in archives),
                "threads": actual.threads,
                "unknown_inputs": actual.unresolved_inputs,
                "retained_documents": actual.retained_documents,
                "all_original_backup_files": len(before),
                "atomic_exchange": True,
                "human_read_memberships_preserved": True,
                "native_sessions_and_diagnostics_preserved": True,
            },
            indent=2,
        ),
        flush=True,
    )
print(
    "PASS: actual retained-root staging, installation and reopening; owned copies removed",
    flush=True,
)
