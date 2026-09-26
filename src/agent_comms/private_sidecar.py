"""Bounded, atomically replaced private SQLite snapshots (POSIX only).

SQLite never opens a caller-controlled filename: it operates on the exact bytes
read through a no-follow, owner-only, single-link descriptor. One pinned-directory
lock serializes readers, installers and writers; complete schema verification
uses that same in-memory connection. Commits publish a fsynced replacement, then
fsync its directory. An intent marker blocks automatic reuse after uncertainty.

This is deliberately a bounded pilot store, not a scalable append database:
each changed snapshot is rewritten in full. Direct SQLite writers are unsupported;
all cooperating writers must use this protocol. It is not an OS sandbox against
arbitrary code running as the same uid, nor protection from a malicious filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .coordination_store import IdentityConflict

_MAX_SIDECAR_BYTES = 32 * 1024 * 1024


class SidecarCommitUnknown(IdentityConflict):
    """No successful durability receipt; callers must not send or retry input."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _require_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise IdentityConflict("Private sidecar file must be regular, owner-only and unaliased.")


@dataclass
class _PinnedDirectory:
    path: Path
    fd: int
    ancestors: tuple[tuple[Path, tuple[int, int]], ...]
    lock_name: str | None = None
    lock_identity: tuple[int, int] | None = None

    def check(self) -> None:
        for path, identity in self.ancestors:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or _identity(info) != identity:
                raise IdentityConflict("Private sidecar directory identity changed.")
            if info.st_uid not in (0, os.geteuid()) or (
                info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
            ):
                raise IdentityConflict("Private sidecar ancestor permissions changed.")
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise IdentityConflict("Private sidecar directory must be owner-only.")
        if self.lock_name is not None:
            locked = self.stat(self.lock_name)
            if locked is None or _identity(locked) != self.lock_identity:
                raise IdentityConflict("Private sidecar lock identity changed.")
            _require_file(locked)

    def stat(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def unchanged(self, name: str, expected: os.stat_result | None) -> None:
        self.check()
        actual = self.stat(name)
        if expected is None:
            if actual is not None:
                raise IdentityConflict("Private sidecar appeared during installation.")
        elif actual is None or _identity(actual) != _identity(expected):
            raise IdentityConflict("Private sidecar inode changed.")
        else:
            _require_file(actual)
            if (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns) != (
                expected.st_size,
                expected.st_mtime_ns,
                expected.st_ctime_ns,
            ):
                raise IdentityConflict("Private sidecar changed outside the snapshot protocol.")


@contextmanager
def _locked_directory(path: Path, *, blocking: bool = True) -> Iterator[_PinnedDirectory]:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise IdentityConflict("Private sidecar snapshots require POSIX no-follow descriptors.")
    import fcntl

    path = path.absolute()
    if ".." in path.parts or not path.name:
        raise IdentityConflict("Private sidecar path must be lexical.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        fd = os.open(path.anchor, flags)
        stack.callback(os.close, fd)
        current = Path(path.anchor)
        ancestors = [(current, _identity(os.fstat(fd)))]
        for part in path.parent.parts[1:]:
            fd = os.open(part, flags, dir_fd=fd)
            stack.callback(os.close, fd)
            current /= part
            ancestors.append((current, _identity(os.fstat(fd))))
        directory = _PinnedDirectory(path.parent, fd, tuple(ancestors))
        directory.check()
        lock_name = f".{path.name}.snapshot-lock"
        lock = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=fd,
        )
        stack.callback(os.close, lock)
        _require_file(os.fstat(lock))
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        stack.callback(fcntl.flock, lock, fcntl.LOCK_UN)
        directory.unchanged(lock_name, os.fstat(lock))
        directory.lock_name = lock_name
        directory.lock_identity = _identity(os.fstat(lock))
        if directory.stat(f".{path.name}.pending") is not None:
            raise SidecarCommitUnknown("Private sidecar has an unresolved commit intent; no retry.")
        yield directory
        directory.unchanged(lock_name, os.fstat(lock))


def _read_snapshot(directory: _PinnedDirectory, name: str) -> tuple[bytes, os.stat_result] | None:
    # O_NONBLOCK ensures a FIFO substitution cannot hang before fstat rejects it.
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        _require_file(info)
        if not 100 <= info.st_size <= _MAX_SIDECAR_BYTES:
            raise IdentityConflict("Private sidecar snapshot is empty or oversized.")
        directory.unchanged(name, info)
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise IdentityConflict("Private sidecar snapshot is truncated.")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        directory.unchanged(name, info)
        if raw[:16] != b"SQLite format 3\x00" or raw[18:20] != b"\x01\x01":
            raise IdentityConflict("Private sidecar must be a standalone rollback-mode snapshot.")
        return raw, info
    finally:
        os.close(fd)


def _connect(raw: bytes | None) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        if raw is not None:
            connection.deserialize(raw)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _verify_schema(
    connection: sqlite3.Connection, ddl: tuple[tuple[str, str], ...], digest: str
) -> None:
    # Include *all* user objects, regardless of name or object type. In particular,
    # an unprefixed trigger attached to a protected table must never be invisible.
    actual = {
        row["name"]: row["sql"] for row in connection.execute("SELECT name,sql FROM sqlite_master")
    }
    if actual != dict(ddl) or connection.execute("SELECT 1 FROM sqlite_temp_master").fetchone():
        raise IdentityConflict("Sidecar schema objects have drifted.")
    rows = connection.execute(f"SELECT singleton,version,ddl_digest FROM {ddl[0][0]}").fetchall()
    if len(rows) != 1 or tuple(rows[0]) != (1, 1, digest):
        raise IdentityConflict("Sidecar schema version differs.")
    databases = connection.execute("PRAGMA database_list").fetchall()
    if not databases or any(
        row["name"] not in {"main", "temp"} or row["file"] != "" for row in databases
    ):
        raise IdentityConflict("Sidecar connection must contain only its in-memory snapshot.")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise IdentityConflict("Sidecar foreign-key checking is disabled.")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise IdentityConflict("Sidecar snapshot integrity failed.")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("Short private sidecar write")
        view = view[count:]


def _publish(
    directory: _PinnedDirectory, name: str, expected: os.stat_result | None, raw: bytes
) -> None:
    if not 100 <= len(raw) <= _MAX_SIDECAR_BYTES:
        raise IdentityConflict("Private sidecar snapshot exceeds its bounded capacity.")
    directory.unchanged(name, expected)
    intent = f".{name}.pending"
    staging = f".{name}.{secrets.token_hex(16)}.staging"
    # An intent is never auto-repaired. Failure/crash after this point requires
    # explicit diagnosis, never replay of a reserved input or a guessed rollback.
    begun = False
    try:
        marker = os.open(
            intent, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd
        )
        begun = True
        try:
            _write_all(marker, b"sidecar-snapshot-v1\n")
            os.fsync(marker)
        finally:
            os.close(marker)
        os.fsync(directory.fd)
        fd = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory.fd,
        )
        try:
            _write_all(fd, raw)
            os.fsync(fd)
            staged = os.fstat(fd)
            _require_file(staged)
        finally:
            os.close(fd)
        directory.unchanged(name, expected)
        directory.unchanged(staging, staged)
        os.replace(staging, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        os.fsync(directory.fd)
        # Content is durable before the uncertainty marker can disappear.
        directory.check()
        actual = directory.stat(name)
        if actual is None or _identity(actual) != _identity(staged):
            raise IdentityConflict("Private sidecar replacement identity changed.")
        _require_file(actual)
        os.unlink(intent, dir_fd=directory.fd)
        os.fsync(directory.fd)
    except BaseException as error:
        if begun:
            raise SidecarCommitUnknown(
                "Private sidecar commit is uncertain; do not send or retry."
            ) from error
        raise


def create_sidecar_file(path: Path, ddl: tuple[tuple[str, str], ...], digest: str) -> None:
    """Serialized create-or-verify; an existing empty/damaged file is not repaired."""
    with _locked_directory(path) as directory:
        snapshot = _read_snapshot(directory, path.name)
        if snapshot is not None:
            raw, identity = snapshot
            connection = _connect(raw)
            try:
                _verify_schema(connection, ddl, digest)
                directory.unchanged(path.name, identity)
            finally:
                connection.close()
            return
        connection = _connect(None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for _, statement in ddl:
                connection.execute(statement)
            connection.execute(f"INSERT INTO {ddl[0][0]} VALUES(1,1,?)", (digest,))
            _verify_schema(connection, ddl, digest)
            connection.execute("COMMIT")
            _publish(directory, path.name, None, connection.serialize())
        finally:
            connection.close()


def verify_sidecar(path: Path, ddl: tuple[tuple[str, str], ...], digest: str) -> None:
    with sidecar_connection(path, ddl, digest):
        pass


@contextmanager
def sidecar_connection(
    path: Path, ddl: tuple[tuple[str, str], ...], digest: str, *, blocking: bool = True
) -> Iterator[sqlite3.Connection]:
    """Verify and use one exact snapshot, committing only on successful scope exit.

    Callers must check rowcount/readback for their own exact inserted identities.
    SQL COMMIT alone is not a durable receipt: the scope must also exit normally.
    """
    with _locked_directory(path, blocking=blocking) as directory:
        snapshot = _read_snapshot(directory, path.name)
        if snapshot is None:
            raise IdentityConflict("Sidecar store is not installed.")
        raw, identity = snapshot
        connection = _connect(raw)
        try:
            _verify_schema(connection, ddl, digest)
            changes = connection.total_changes
            yield connection
            if connection.in_transaction:
                raise IdentityConflict("Sidecar scope left an unfinished transaction.")
            _verify_schema(connection, ddl, digest)
            directory.unchanged(path.name, identity)
            if connection.total_changes != changes:
                _publish(directory, path.name, identity, connection.serialize())
        finally:
            connection.close()


def encode_request(text: str) -> str:
    """JSON-encode like the pinned native ``_claimNativeInput`` request."""
    return json.dumps(
        {
            "kind": "prompt",
            "text": text,
            "images": None,
            "streamingBehavior": None,
            "expandPromptTemplates": True,
            "source": "interactive",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def native_request_digest(text: str) -> str:
    """sha256 over the pinned native request envelope, not the bare text."""
    return hashlib.sha256(
        ("pi-input-request-v1\n" + encode_request(text)).encode("utf-8")
    ).hexdigest()
