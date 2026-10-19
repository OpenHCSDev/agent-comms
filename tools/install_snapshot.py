#!/usr/bin/env python3
"""Read-only snapshot producer. Run ONLY inside the gate's read-only sandbox.

Stdout is a zstd tar stream. No scratch files, lock creation, SQLite writes or
imports of bus/runtime code. This same source is streamed over SSH.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
from contextlib import closing
from pathlib import Path


def add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    entry = tarfile.TarInfo(name)
    entry.mode = 0o600
    entry.size = len(payload)
    archive.addfile(entry, io.BytesIO(payload))


def sqlite_snapshot(path: Path) -> bytes:
    """WAL readers use SQLite's pager; closed checkpointed stores need no SHM.

    SQLite otherwise tries to create missing WAL/SHM files even in mode=ro.
    immutable is safe only with no journal/WAL at either cut and an unchanged
    main inode. A concurrent checkpoint or first writer makes this fail closed.
    """
    companions = [path.with_name(path.name + suffix) for suffix in ("-wal", "-journal")]
    immutable = not any(companion.exists() for companion in companions)
    before = path.stat()
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
            db.execute("BEGIN")
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            payload = db.serialize()
    except sqlite3.Error as error:
        raise RuntimeError(f"Read-only SQLite snapshot failed for {path}: {error}") from error
    if immutable:
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns
        ) or any(companion.exists() for companion in companions):
            raise RuntimeError(f"Checkpointed SQLite store changed while snapshotting: {path}")
    return payload


def snapshot(root: Path, output) -> None:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Bus root is not a directory")
    home = Path.home()
    route = home / ".local/state/agent-comms/active-route.json"
    pointer = home / ".local/share/agent-comms/current"
    context = {"root": str(root), "route": json.loads(route.read_text()) if route.exists() else None,
               "runtime": str(pointer.resolve()) if pointer.exists() else None}
    with tarfile.open(fileobj=output, mode="w|zst", level=1) as archive:
        add_bytes(archive, "context.json", json.dumps(context).encode())
        for directory, folders, names in os.walk(root, followlinks=False):
            for name in sorted([*folders, *names]):
                path = Path(directory) / name
                info = path.lstat()
                relative = str(Path("bus") / path.relative_to(root))
                if stat.S_ISSOCK(info.st_mode):
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"Refusing snapshot symlink: {path}")
                if stat.S_ISDIR(info.st_mode):
                    entry = tarfile.TarInfo(relative)
                    entry.type = tarfile.DIRTYPE
                    entry.mode = 0o700
                    archive.addfile(entry)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"Refusing special file: {path}")
                # Companions belong to the SQLite pager, not to the file copier.
                if path.name.endswith(("-wal", "-shm", "-journal")):
                    primary = path.with_name(path.name.rsplit("-", 1)[0])
                    if primary.is_file():
                        with primary.open("rb") as source:
                            if source.read(16) == b"SQLite format 3\x00":
                                continue
                with path.open("rb") as source:
                    if source.read(16) == b"SQLite format 3\x00":
                        add_bytes(archive, relative, sqlite_snapshot(path))
                    else:
                        # The opened inode survives atomic replacements. Appends after
                        # this cut are not read; truncation makes tar fail, never pass.
                        source.seek(0)
                        cut = os.fstat(source.fileno())
                        entry = tarfile.TarInfo(relative)
                        entry.mode = 0o600
                        entry.size = cut.st_size
                        archive.addfile(entry, source)
                        after = os.fstat(source.fileno())
                        if after.st_size < cut.st_size or (
                            after.st_size == cut.st_size and after.st_mtime_ns != cut.st_mtime_ns
                        ):
                            raise ValueError(f"File rewritten while snapshotting: {path}")


if __name__ == "__main__":
    snapshot(Path(sys.argv[1]), sys.stdout.buffer)
