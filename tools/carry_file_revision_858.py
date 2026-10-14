#!/usr/bin/env python3
"""One-time carry of pre-#858 file revisions into FileRevision records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_comms.activity_checkpoint import ActivityCheckpoint
from agent_comms.bus_page_index import BusPageSource
from agent_comms.bus_projection import AppendCheckpoint
from agent_comms.bus_route_counts import RouteSourceRow
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import FileIdentity, FileRevision
from agent_comms.view_unread import ReplyIndex


@dataclass(frozen=True)
class StagedFile:
    destination: Path
    staged: Path
    remove_after: Path | None = None


class CarryRefused(RuntimeError):  # noqa: N818 - names a refused one-shot operation
    pass


def _revision(
    path: Path,
    inode: int,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    *,
    device: int | None = None,
) -> FileRevision:
    observed = path.stat()
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_ino != inode
        or (device is not None and observed.st_dev != device)
        or observed.st_size < size
    ):
        raise CarryRefused(f"{path}: recorded revision belongs to another file")
    return FileRevision(
        FileIdentity(observed.st_dev, observed.st_ino), size, mtime_ns, ctime_ns
    )


def _old_four(value: object, label: str) -> tuple[int, int, int, int]:
    if not (
        isinstance(value, list)
        and len(value) == 4
        and all(type(item) is int and item >= 0 for item in value)
    ):
        raise CarryRefused(f"{label}: expected one old four-integer revision")
    return tuple(value)  # type: ignore[return-value]


def _old_five(value: object, label: str) -> tuple[int, int, int, int, int]:
    if not (
        isinstance(value, list)
        and len(value) == 5
        and all(type(item) is int and item >= 0 for item in value)
    ):
        raise CarryRefused(f"{label}: expected one old five-integer revision")
    return tuple(value)  # type: ignore[return-value]


def _old_bus(value: object, label: str) -> tuple[int, int, int, int]:
    if not isinstance(value, dict) or set(value) != {"inode", "size", "modified", "changed"}:
        raise CarryRefused(f"{label}: expected one old opened-bus revision")
    values = tuple(value[name] for name in ("inode", "size", "modified", "changed"))
    if not all(type(item) is int and item >= 0 for item in values):
        raise CarryRefused(f"{label}: invalid opened-bus revision")
    return values  # type: ignore[return-value]


def _encoded(revision: FileRevision) -> dict:
    encoded = FieldCodec.encode(revision)
    FieldCodec.decode(FileRevision, encoded)
    return encoded


def _tail(path: Path, offset: int) -> str:
    with path.open("rb") as source:
        source.seek(max(0, offset - 4096))
        return hashlib.sha256(source.read(offset - source.tell())).hexdigest()


def _same_owner_processes(root: Path) -> tuple[str, ...]:
    registry_path = root / "registry.json"
    try:
        registry = json.loads(registry_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise CarryRefused(f"{registry_path}: cannot verify stopped owners") from error
    active = []
    for name, thread in registry.get("threads", {}).items():
        raw = thread.get("process_identity") if isinstance(thread, dict) else None
        if raw is None:
            continue
        try:
            identity = ProcessIdentity(raw["pid"], raw["start_time"])
        except (KeyError, TypeError, ValueError) as error:
            raise CarryRefused(f"{registry_path}: invalid process identity for {name}") from error
        if not identity.alive():
            continue
        try:
            environment = Path(f"/proc/{identity.pid}/environ").read_bytes().split(b"\0")
            values = dict(item.split(b"=", 1) for item in environment if b"=" in item)
            process_root = Path(os.fsdecode(values[b"AGENT_COMMS_ROOT"])).resolve()
        except (FileNotFoundError, KeyError, OSError, ValueError) as error:
            raise CarryRefused(
                f"{registry_path}: live owner {name} cannot be separated from this root"
            ) from error
        if process_root == root.resolve():
            active.append(f"{name}:{identity.pid}")
    return tuple(active)


def _stage_bytes(path: Path, payload: bytes) -> StagedFile:
    info = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.carry-858-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(output.fileno(), info.st_uid, info.st_gid)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return StagedFile(path, staged)


def _stage_json(path: Path, value: object) -> StagedFile:
    return _stage_bytes(path, json.dumps(value, separators=(",", ":")).encode())


def _stage_database(path: Path) -> Path:
    for suffix in ("-journal", "-wal"):
        companion = path.with_name(path.name + suffix)
        if companion.exists() and companion.stat().st_size:
            raise CarryRefused(f"{path}: database has uncheckpointed {suffix} content")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.carry-858-", dir=path.parent)
    os.close(descriptor)
    staged = Path(temporary)
    staged.unlink()
    try:
        shutil.copy2(path, staged)
        source = path.stat()
        os.chmod(staged, stat.S_IMODE(source.st_mode))
        os.chown(staged, source.st_uid, source.st_gid)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _sync_database(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_marker(bus_root: Path) -> StagedFile:
    marker_path = bus_root / "bus_meta.json"
    checkpoint_path = bus_root / "private_bus_checkpoint.sqlite3"
    bus_path = bus_root / "bus.jsonl"
    marker = json.loads(marker_path.read_text())
    seal = marker.get("checkpoint_seal")
    if not isinstance(seal, dict) or seal.get("state") != "final":
        raise CarryRefused(f"{marker_path}: final checkpoint seal required")
    old_db = _old_five(seal.get("db_revision"), f"{marker_path}:db_revision")
    witness = seal.get("witness")
    if not isinstance(witness, dict):
        raise CarryRefused(f"{marker_path}: final witness is absent")
    old_bus = _old_five(witness.get("revision"), f"{marker_path}:witness.revision")
    db_revision = _revision(
        checkpoint_path, old_db[1], old_db[2], old_db[3], old_db[4], device=old_db[0]
    )
    bus_revision = _revision(
        bus_path, old_bus[1], old_bus[2], old_bus[3], old_bus[4], device=old_bus[0]
    )
    if (
        bus_revision.size != bus_path.stat().st_size
        or _tail(bus_path, old_bus[2]) != witness.get("tail")
    ):
        raise CarryRefused(f"{marker_path}: recorded bus revision differs from source")
    connection = sqlite3.connect(f"file:{checkpoint_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT device,inode,offset,mtime_ns,ctime_ns FROM prefix_certificate "
            "WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    if row != old_bus:
        raise CarryRefused(f"{checkpoint_path}: certificate differs from marker witness")
    seal["db_revision"] = _encoded(db_revision)
    seal["witness"]["revision"] = _encoded(bus_revision)
    return _stage_json(marker_path, marker)


def _stage_activity(root: Path) -> StagedFile | None:
    path = root / "activity_latest.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    revision = _old_four(value.get("source"), f"{path}:source")
    value["source"] = _encoded(_revision(root / "activity.jsonl", *revision))
    FieldCodec.decode(ActivityCheckpoint, value)
    return _stage_json(path, value)


def _stage_append_checkpoint(path: Path, bus_path: Path) -> StagedFile:
    value = json.loads(path.read_text())
    revision = _old_bus(value.get("source"), f"{path}:source")
    value["source"] = _encoded(_revision(bus_path, *revision))
    value["integrity"] = AppendCheckpoint.payload_digest(value)
    return _stage_json(path, value)


def _stage_history_sources(root: Path) -> StagedFile | None:
    path = root / "history_sources.json"
    if not path.exists():
        return None
    values = json.loads(path.read_text())
    if not isinstance(values, list) or not values:
        raise CarryRefused(f"{path}: expected old history source records")
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise CarryRefused(f"{path}:{index}: invalid source")
        source_root = Path(value.get("root", ""))
        for field, name in (
            ("snapshot_bus_revision", "bus.jsonl"),
            ("snapshot_registry_revision", "registry.json"),
        ):
            revision = _old_four(value.get(field), f"{path}:{index}:{field}")
            value[field] = _encoded(_revision(source_root / name, *revision))
    return _stage_json(path, values)


def _stage_bus_page(bus_root: Path) -> StagedFile | None:
    path = bus_root / "bus_page_index.sqlite3"
    if not path.exists():
        return None
    staged = _stage_database(path)
    connection = sqlite3.connect(staged)
    try:
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(bus_page_source)"))
        if columns != ("identity", "offset", "tail", "singleton"):
            raise CarryRefused(f"{path}: page source is already new-format or unknown")
        identity, offset, tail, singleton = connection.execute(
            "SELECT identity,offset,tail,singleton FROM bus_page_source"
        ).fetchone()
        device, inode, mtime_ns, ctime_ns = _old_four(
            json.loads(identity), f"{path}:identity"
        )
        revision = _revision(
            bus_root / "bus.jsonl", inode, offset, mtime_ns, ctime_ns, device=device
        )
        with connection:
            connection.execute("ALTER TABLE bus_page_source RENAME TO old_bus_page_source")
            BusPageSource.create(connection)
            BusPageSource(revision, tail, singleton).insert(connection)
            connection.execute("DROP TABLE old_bus_page_source")
    finally:
        connection.close()
    _sync_database(staged)
    return StagedFile(path, staged)


def _stage_route_counts(root: Path) -> StagedFile | None:
    path = root / "bus_route_counts.sqlite3"
    if not path.exists():
        return None
    staged = _stage_database(path)
    connection = sqlite3.connect(staged)
    try:
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(route_source)"))
        if columns != ("id", "identity", "offset", "tail_digest"):
            raise CarryRefused(f"{path}: route source is already new-format or unknown")
        identifier, identity, offset, tail = connection.execute(
            "SELECT id,identity,offset,tail_digest FROM route_source"
        ).fetchone()
        if identity is None:
            revision = None
        else:
            device, inode, mtime_ns, ctime_ns = _old_four(
                json.loads(identity), f"{path}:identity"
            )
            revision = _revision(
                root / "bus.jsonl", inode, offset, mtime_ns, ctime_ns, device=device
            )
        with connection:
            connection.execute("ALTER TABLE route_source RENAME TO old_route_source")
            RouteSourceRow.create(connection)
            RouteSourceRow(identifier, revision, tail).insert(connection)
            connection.execute("DROP TABLE old_route_source")
    finally:
        connection.close()
    _sync_database(staged)
    return StagedFile(path, staged)


def _stage_transcript_index(root: Path) -> StagedFile | None:
    old_path = root / "transcript_reply_index.v3.sqlite3"
    new_path = root / "transcript_reply_index.v4.sqlite3"
    if new_path.exists():
        raise CarryRefused(f"{new_path}: transcript index is already new-format")
    if not old_path.exists():
        return None
    staged = _stage_database(old_path)
    connection = sqlite3.connect(staged)
    try:
        if connection.execute("PRAGMA user_version").fetchone() != (3,):
            raise CarryRefused(f"{old_path}: expected transcript index version 3")
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(reply_index)"))
        if columns != ("revision", "through", "total", "source", "complete"):
            raise CarryRefused(f"{old_path}: transcript index schema is unknown")
        rows = connection.execute(
            "SELECT revision,through,total,source,complete FROM reply_index"
        ).fetchall()
        converted = []
        for raw, through, total, source, complete in rows:
            if raw is None:
                revision = None
            else:
                old_revision = _old_four(json.loads(raw), f"{old_path}:{source}")
                revision = _revision(Path(source), *old_revision)
            converted.append(ReplyIndex(revision, through, total, source, bool(complete)))
        with connection:
            connection.execute("ALTER TABLE reply_index RENAME TO old_reply_index")
            ReplyIndex.create(connection)
            for row in converted:
                row.insert(connection)
            connection.execute("DROP TABLE old_reply_index")
            connection.execute("PRAGMA user_version=4")
    finally:
        connection.close()
    _sync_database(staged)
    return StagedFile(new_path, staged, remove_after=old_path)


def _bus_roots(root: Path) -> tuple[Path, ...]:
    historical = tuple(sorted(
        path.parent for path in (root / "history").glob("*/bus_meta.json")
    )) if (root / "history").is_dir() else ()
    return root, *historical


def stage(root: Path) -> list[StagedFile]:
    if active := _same_owner_processes(root):
        raise CarryRefused("active owners hold root: " + ", ".join(active))
    staged: list[StagedFile] = []
    try:
        for bus_root in _bus_roots(root):
            staged.append(_stage_marker(bus_root))
            page = _stage_bus_page(bus_root)
            if page is not None:
                staged.append(page)
        for prepare in (
            _stage_activity,
            _stage_history_sources,
            _stage_route_counts,
            _stage_transcript_index,
        ):
            optional = prepare(root)
            if optional is not None:
                staged.append(optional)
        bus_path = root / "bus.jsonl"
        for path in (root / "bus_activity_latest.json", *sorted(root.glob("bus_display_*.json"))):
            if path.exists():
                staged.append(_stage_append_checkpoint(path, bus_path))
        return staged
    except BaseException:
        for item in staged:
            item.staged.unlink(missing_ok=True)
        for temporary in root.rglob(".*.carry-858-*"):
            temporary.unlink(missing_ok=True)
        raise


def publish(staged: list[StagedFile]) -> None:
    markers = [item for item in staged if item.destination.name == "bus_meta.json"]
    ordinary = [item for item in staged if item.destination.name != "bus_meta.json"]
    for item in (*ordinary, *markers):
        os.replace(item.staged, item.destination)
        directory = os.open(item.destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if item.remove_after is not None:
            item.remove_after.unlink()
            directory = os.open(item.remove_after.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        print(item.destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args()
    root = arguments.root.expanduser().resolve()
    try:
        staged = stage(root)
        publish(staged)
    except CarryRefused as error:
        parser.error(str(error))
    print(f"converted {len(staged)} stores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
