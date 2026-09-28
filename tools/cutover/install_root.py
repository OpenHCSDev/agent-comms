"""Install a staged D22 root atomically while all affected clients are closed.

This one-shot operator keeps the original directory as its rollback backup.
It never stops a client, starts an owner, changes launchers or sends an input.
Delete this tool after the real installation succeeds.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import filecmp
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing, contextmanager
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path

import psutil
from current_root import RUNTIME_DATABASES, RootRehearsal

from agent_comms.active_route import read_active_route
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.store_files import _atomic_write_text, _store_lock, file_revision
from agent_comms.wire_log import WireLog
from agent_comms.wire_metadata import ArchivedAccess, WireAccess, WireMetadata, WritableAccess


def receipt(candidate: Path) -> RootRehearsal:
    return FieldCodec.decode(RootRehearsal, json.loads((candidate / "rehearsal.json").read_text()))


def assert_source_unchanged(source: Path, expected: RootRehearsal) -> None:
    if str(source) != expected.source:
        raise ValueError("Candidate belongs to a different source root")
    for name, revision in expected.source_revisions.items():
        if file_revision(source / name) != revision:
            raise ValueError(f"Source changed since staging: {source / name}")


def assert_quiet(source: Path) -> None:
    """Check registered owners, explicit roots, open files and default Toad clients."""
    raw = json.loads((source / "registry.json").read_text())
    for row in raw["threads"].values():
        pid = row.get("pid", 0)
        if pid and psutil.pid_exists(pid):
            raise ValueError(f"Registered owner is still alive: {row['name']}")
    active = read_active_route()
    default_root = active is not None and active.root.resolve() == source
    executables = {"toad", "agent-comms", "agent-comms-acp", "agent-comms-agent"}
    executables.update(
        Path(row["agent_bin"]).name for row in raw["threads"].values() if row.get("agent_bin")
    )
    for process in psutil.process_iter(("pid", "name", "uids")):
        try:
            if process.pid == os.getpid() or process.uids().real != os.geteuid():
                continue
            environment_root = process.environ().get("AGENT_COMMS_ROOT")
            attached = environment_root and Path(environment_root).resolve() == source
            if attached or (default_root and process.info["name"] == "toad"):
                raise ValueError(f"Affected client is still alive: {process.pid}")
            if any(Path(item.path).is_relative_to(source) for item in process.open_files()):
                raise ValueError(f"Process still has source files open: {process.pid}")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            # Privileged user services can deny environ/fd inspection. They do
            # not block an unrelated root; a recognizable Comms client does.
            try:
                command = process.cmdline()
            except psutil.NoSuchProcess:
                continue
            if any(Path(arg).name in executables or str(source) in arg for arg in command):
                raise ValueError(f"Cannot inspect Comms client {process.pid}") from error


def ignore_sockets(directory: str, names: list[str]) -> list[str]:
    return [name for name in names if stat.S_ISSOCK((Path(directory) / name).lstat().st_mode)]


def sync_tree(root: Path) -> None:
    for path in (*root.rglob("*"), root):
        if path.is_symlink():
            continue
        if not (path.is_file() or path.is_dir()):
            raise ValueError(f"Unexpected special file in prepared root: {path}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def exchange(first: Path, second: Path) -> None:
    """Linux exchanges directories without exposing a missing or half-written root."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(first), -100, os.fsencode(second), 2):
        raise OSError(ctypes.get_errno(), "Atomic root exchange failed")


@contextmanager
def source_lock(source: Path):
    """Use the actual writer lock without decoding the retired source schema."""
    with (source / ".bus.jsonl.lock").open("a+b") as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield


def overlay(candidate: Path, prepared: Path, access: WireAccess) -> RootRehearsal:
    """Replace converted stores; preserve untouched sessions, diagnostics and documents."""
    expected = receipt(candidate)
    old_indexes = (
        *RUNTIME_DATABASES,
        "private_bus_checkpoint.sqlite3",
        "wake_candidates.sqlite3",
        "bus_page_index.sqlite3",
        "bus_route_counts.sqlite3",
        "runtime_info.json",
        "owner_release_receipts.json",
        "activity.jsonl",
        "activity_latest.json",
        "bus_activity_latest.json",
        "read_markers.json",
        "thread_read_markers.json",
        "acp_delivery_cursors.json",
        "acp_passive_channel_awareness.json",
        "goal_waits.json",
    )
    for name in old_indexes:
        for suffix in ("", "-wal", "-shm", "-journal"):
            (prepared / (name + suffix)).unlink(missing_ok=True)
    for pattern in ("transcript_reply_index*.sqlite3*", "bus_display_*.json"):
        for path in prepared.glob(pattern):
            path.unlink()
    runtime = prepared / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    shutil.copytree(
        candidate / "precutover-evidence", prepared / "precutover-evidence", dirs_exist_ok=True
    )
    for path in candidate.iterdir():
        if (
            path.is_file()
            and path.name
            not in {
                "rehearsal.json",
                "rewrite-receipt.json",
                "history_sources.json",
                "private_bus_checkpoint.sqlite3",
                "wake_candidates.sqlite3",
            }
            and not path.name.endswith(("-wal", "-shm", "-journal", ".lock"))
        ):
            if path.suffix == ".sqlite3":
                for suffix in ("-wal", "-shm", "-journal"):
                    (prepared / (path.name + suffix)).unlink(missing_ok=True)
            shutil.copy2(path, prepared / path.name)
    log = WireLog(prepared / "bus.jsonl")
    marker = FieldCodec.decode(WireMetadata, json.loads(log.metadata_path.read_text()))
    log.write_metadata_unlocked(replace(marker, checkpoint_version=None, checkpoint_seal=None))
    install_private_bus_checkpoint(log)
    read_path = prepared / ReadLedger.filename
    if read_path.exists():
        reads = FieldCodec.decode(ReadDocument, json.loads(read_path.read_text()))
        transcripts = {}
        original = Path(expected.source)
        for key, through in reads.transcripts.items():
            viewer, filename, inode = FieldCodec.decode(tuple[str, str, int], json.loads(key))
            path = Path(filename)
            if path.is_relative_to(original) and path.exists() and path.stat().st_ino == inode:
                copied = prepared / path.relative_to(original)
                key = ReadLedger._transcript_key(viewer, filename, copied.stat().st_ino)
            transcripts[key] = through
        reads = replace(reads, transcripts=transcripts)
        if reads.bus_identity == ReadLedger.bus_identity(candidate / "bus.jsonl"):
            reads = replace(reads, bus_identity=ReadLedger.bus_identity(log.path))
        _atomic_write_text(read_path, json.dumps(FieldCodec.encode(reads)))
    verify(prepared, candidate, expected, access)
    return expected


def verify(root: Path, candidate: Path, expected: RootRehearsal, access: WireAccess) -> None:
    current = Comms(root)
    if len(current.bus.log.full_history()) != expected.messages:
        raise ValueError("Installed message count differs from the staged history")
    if len(current.registry.all_threads()) != expected.threads:
        raise ValueError("Installed registry differs from the staged history")
    inputs = FieldCodec.decode(
        InputDocument, json.loads((root / InputDispositions.filename).read_text())
    )
    if sum(row.unresolved for row in inputs.rows.values()) != expected.unresolved_inputs:
        raise ValueError("Installed unresolved inputs differ from the staged history")
    staged_inputs = FieldCodec.decode(
        InputDocument, json.loads((candidate / InputDispositions.filename).read_text())
    )
    if inputs != staged_inputs:
        raise ValueError("Installed input identities/outcomes differ from the staged history")
    staged = Comms(candidate)
    marker = current.bus.log.read_metadata_unlocked(required=True)
    staged_marker = staged.bus.log.read_metadata_unlocked(required=True)
    if marker.access != access:
        raise ValueError("Installed root does not have its assigned archive or writer access")
    if (
        replace(marker, checkpoint_version=None, checkpoint_seal=None)
        != replace(staged_marker, checkpoint_version=None, checkpoint_seal=None)
        or marker.admission_after_seq != expected.admission_after_seq
    ):
        raise ValueError("Installed access, identity or admission floor differs from the candidate")
    if current.registry.store.read() != staged.registry.store.read():
        raise ValueError("Installed owner, goal or generation data differs from the candidate")
    if not filecmp.cmp(root / "bus.jsonl", candidate / "bus.jsonl", shallow=False):
        raise ValueError("Installed wire differs from the staged complete history")
    for staged_database in candidate.glob("*.sqlite3"):
        if staged_database.name in {"private_bus_checkpoint.sqlite3", "wake_candidates.sqlite3"}:
            continue
        verify_database(staged_database, root / staged_database.name)
    for name in expected.retained_documents:
        if not name.endswith(".sqlite3") and not filecmp.cmp(
            root / name, candidate / name, shallow=False
        ):
            raise ValueError(f"Installed retained document differs: {name}")


def verify_database(candidate: Path, installed: Path) -> None:
    """Compare complete converted schemas and rows, streaming each declared table."""
    with (
        closing(sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)) as before,
        closing(sqlite3.connect(installed.as_uri() + "?mode=ro", uri=True)) as after,
    ):
        schema_query = (
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        schema = before.execute(schema_query).fetchall()
        if after.execute(schema_query).fetchall() != schema:
            raise ValueError(f"Installed database schema differs: {installed.name}")
        if after.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError(f"Installed database is corrupt: {installed.name}")
        for kind, name, _sql in schema:
            if kind != "table":
                continue
            identifier = '"' + name.replace('"', '""') + '"'
            width = len(before.execute(f"PRAGMA table_info({identifier})").fetchall())
            query = f"SELECT * FROM {identifier} ORDER BY " + ",".join(
                str(index + 1) for index in range(width)
            )
            if any(
                first != second
                for first, second in zip_longest(before.execute(query), after.execute(query))
            ):
                raise ValueError(f"Installed database rows differ: {installed.name}/{name}")


def install(source: Path, candidate: Path, backup: Path) -> RootRehearsal:
    source, candidate, backup = source.resolve(), candidate.resolve(), backup.absolute()
    if backup.exists() or backup.is_relative_to(source) or candidate.is_relative_to(source):
        raise ValueError("Candidate and new backup must be outside the source")
    if backup.parent.stat().st_dev != source.parent.stat().st_dev:
        raise ValueError("Backup must share the source filesystem for atomic retention")
    expected = receipt(candidate)
    assert_quiet(source)
    assert_source_unchanged(source, expected)
    required = sum(
        path.lstat().st_size
        for directory in (source, candidate)
        for path in directory.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if shutil.disk_usage(source.parent).free < required:
        raise ValueError("Insufficient disk space for the measured source and candidate copies")
    prepared = Path(tempfile.mkdtemp(prefix=".agent-comms-install-", dir=source.parent))
    exchanged = False
    try:
        with source_lock(source):
            assert_quiet(source)
            shutil.copytree(
                source, prepared, dirs_exist_ok=True, symlinks=True, ignore=ignore_sockets
            )
            overlay(candidate, prepared, WritableAccess())
            staged_sources = FieldCodec.decode(
                tuple[HistorySource, ...],
                json.loads((candidate / "history_sources.json").read_text()),
            )
            installed_sources = []
            for item in staged_sources:
                archive_candidate = Path(item.root)
                archive_receipt = receipt(archive_candidate)
                archive_source = Path(archive_receipt.source)
                assert_source_unchanged(archive_source, archive_receipt)
                relative = archive_source.relative_to(source)
                archive_prepared = prepared / relative
                overlay(archive_candidate, archive_prepared, ArchivedAccess())
                installed_sources.append(
                    replace(
                        item,
                        root=str(source / relative),
                        snapshot_bus_revision=file_revision(archive_prepared / "bus.jsonl"),
                        snapshot_registry_revision=file_revision(
                            archive_prepared / "registry.json"
                        ),
                    )
                )
            _atomic_write_text(
                prepared / "history_sources.json",
                json.dumps(FieldCodec.encode(tuple(installed_sources))),
            )
            sync_tree(prepared)
            assert_source_unchanged(source, expected)
            for item in staged_sources:
                archive_receipt = receipt(Path(item.root))
                assert_source_unchanged(Path(archive_receipt.source), archive_receipt)
            with _store_lock(prepared / "bus.jsonl"):
                exchange(source, prepared)
                exchanged = True
            # Readers acquire the installed root's wire lock themselves. The
            # prepared lock now names that inode and must be released first.
            try:
                verify(source, candidate, expected, WritableAccess())
                for item in installed_sources:
                    item.validate()
                    item.registry()
            except BaseException:
                exchange(source, prepared)
                exchanged = False
                raise
            os.rename(prepared, backup)
            for parent in {source.parent, backup.parent}:
                descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return expected
    finally:
        if not exchanged and prepared.exists():
            shutil.rmtree(prepared)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(FieldCodec.encode(install(args.source, args.candidate, args.backup)), indent=2)
    )
