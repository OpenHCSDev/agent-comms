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
import os
import secrets
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.locked_store import SQLiteFile
from agent_comms.coordination_errors import IdentityConflict

from .field_codec import FieldCodec

if TYPE_CHECKING:
    from .agent_backend import StreamingInputPolicy

from .private_path import ObservedPath, PrivateFileRole, TrustedAncestorRole
from .store_files import ExclusiveLock, StoreOwner
from .typed_table import (
    AllObjectsTables,
    ImmutableRows,
    SingletonRow,
    SQLiteDatabase,
    SQLiteForeignKeys,
    SQLiteSchemaObject,
    TypedRow,
    TypedTable,
)

_MAX_SIDECAR_BYTES = 32 * 1024 * 1024


class UnprivateSidecarFile(IdentityConflict):
    def __init__(self) -> None:
        super().__init__("Private sidecar file must be regular, owner-only and unaliased.")


class SidecarCommitUnknown(IdentityConflict):
    """No successful durability acknowledgement; callers must not send or retry input."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


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
            PrivateFileRole.require(locked, UnprivateSidecarFile())

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
            PrivateFileRole.require(actual, UnprivateSidecarFile())
            if (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns) != (
                expected.st_size,
                expected.st_mtime_ns,
                expected.st_ctime_ns,
            ):
                raise IdentityConflict("Private sidecar changed outside the snapshot protocol.")


@contextmanager
def _locked_directory(path: Path) -> Iterator[_PinnedDirectory]:
    path = path.absolute()
    if ".." in path.parts or not path.name:
        raise IdentityConflict("Private sidecar path must be lexical.")
    with ExitStack() as stack:
        refusal = IdentityConflict("Private sidecar snapshots require a trusted directory.")
        fd = TrustedAncestorRole.read(path.anchor, refusal)
        stack.callback(os.close, fd)
        current = Path(path.anchor)
        ancestors = [(current, _identity(os.fstat(fd)))]
        for part in path.parent.parts[1:]:
            fd = TrustedAncestorRole.read(part, refusal, dir_fd=fd)
            stack.callback(os.close, fd)
            current /= part
            ancestors.append((current, _identity(os.fstat(fd))))
        directory = _PinnedDirectory(path.parent, fd, tuple(ancestors))
        directory.check()
        lock_name = StoreOwner.snapshot_lock(path).name
        lock = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=fd,
        )
        stack.callback(os.close, lock)
        PrivateFileRole.require(os.fstat(lock), UnprivateSidecarFile())
        # Closing the description, already on the stack, gives the hold back.
        ExclusiveLock().wait(lock)
        directory.unchanged(lock_name, os.fstat(lock))
        directory.lock_name = lock_name
        directory.lock_identity = _identity(os.fstat(lock))
        intent = ObservedPath.observe(partial(
            os.stat, StoreOwner.snapshot_intent(path).name,
            dir_fd=directory.fd, follow_symlinks=False,
        ))
        if not intent.is_absent():
            raise SidecarCommitUnknown("Private sidecar has an unresolved commit intent; no retry.")
        yield directory
        directory.unchanged(lock_name, os.fstat(lock))


def _read_snapshot(directory: _PinnedDirectory, name: str) -> tuple[bytes, os.stat_result] | None:
    # O_NONBLOCK ensures a FIFO substitution cannot hang before fstat rejects it.
    try:
        fd = PrivateFileRole.read(name, UnprivateSidecarFile(), dir_fd=directory.fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
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
    connection = SQLiteFile.writer(":memory:", isolation_level=None)
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


@dataclass(frozen=True)
class SnapshotMeta(ImmutableRows, SingletonRow):
    ddl_digest: str

@dataclass(frozen=True)
class _QuickCheck(TypedRow):
    quick_check: str


def _schema(row_type: type[TypedTable]) -> dict[str, str]:
    return SnapshotMeta.schema_objects() | row_type.schema_objects()


def _digest(row_type: type[TypedTable]) -> str:
    return hashlib.sha256(FieldCodec.compact(_schema(row_type)).encode()).hexdigest()


def _verify_schema(connection: sqlite3.Connection, row_type: type[TypedTable]) -> None:
    # Every named SQL object is checked, including triggers without our prefix.
    actual = AllObjectsTables.stored_schema(connection)
    temporary = SQLiteSchemaObject.read(
        connection.execute("SELECT name,sql FROM sqlite_temp_master WHERE sql IS NOT NULL")
    )
    if SQLiteSchemaObject.differences(actual, _schema(row_type)) or temporary:
        raise IdentityConflict("Sidecar schema objects have drifted.")
    if SnapshotMeta.select(connection) != [SnapshotMeta(1, _digest(row_type))]:
        raise IdentityConflict("Sidecar schema declaration differs.")
    databases = SQLiteDatabase.read(connection.execute("PRAGMA database_list"))
    if not databases or any(
        row.name not in {"main", "temp"} or row.file != "" for row in databases
    ):
        raise IdentityConflict("Sidecar connection must contain only its in-memory snapshot.")
    if SQLiteForeignKeys.read(connection.execute("PRAGMA foreign_keys")) != [
        SQLiteForeignKeys(True)
    ]:
        raise IdentityConflict("Sidecar foreign-key checking is disabled.")
    if _QuickCheck.read(connection.execute("PRAGMA quick_check")) != [_QuickCheck("ok")]:
        raise IdentityConflict("Sidecar snapshot integrity failed.")


@dataclass(frozen=True)
class SnapshotIdentity:
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int

    @classmethod
    def of(cls, info: os.stat_result) -> SnapshotIdentity:
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class VerifiedSnapshots:
    """Last verified file identity per sidecar and row type in this process.

    The pinned-directory protocol checks replacement and mutation independently.
    Unchanged snapshots reuse their pass; changed identities require verification.
    Authorized writes preserve the schema and record their published identity.
    """

    passed: ClassVar[dict[tuple[Path, type[TypedTable]], SnapshotIdentity]] = {}

    @classmethod
    def verify(cls, connection: sqlite3.Connection, row_type: type[TypedTable],
               path: Path, info: os.stat_result) -> None:
        identity = SnapshotIdentity.of(info)
        if cls.passed.get((path, row_type)) != identity:
            _verify_schema(connection, row_type)
            cls.published(path, row_type, info)

    @classmethod
    def published(cls, path: Path, row_type: type[TypedTable], info: os.stat_result) -> None:
        cls.passed[path, row_type] = SnapshotIdentity.of(info)


_DENIED_SIDECAR_ACTIONS = frozenset({
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_PRAGMA,
})


def _authorize_sidecar(action: int, *_: object) -> int:
    return sqlite3.SQLITE_DENY if action in _DENIED_SIDECAR_ACTIONS else sqlite3.SQLITE_OK


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("Short private sidecar write")
        view = view[count:]


def _publish(
    directory: _PinnedDirectory, name: str, expected: os.stat_result | None, raw: bytes
) -> os.stat_result:
    if not 100 <= len(raw) <= _MAX_SIDECAR_BYTES:
        raise IdentityConflict("Private sidecar snapshot exceeds its bounded capacity.")
    directory.unchanged(name, expected)
    intent = StoreOwner.snapshot_intent(Path(name)).name
    staging = f".{name}.{secrets.token_hex(16)}.staging"
    # An intent is never auto-repaired. Failure/crash after this point requires
    # explicit diagnosis, never replay of a reserved input or a guessed rollback.
    begun = False
    try:
        marker = PrivateFileRole.create(intent, UnprivateSidecarFile(), dir_fd=directory.fd)
        begun = True
        try:
            _write_all(marker, b"sidecar-snapshot-v1\n")
            os.fsync(marker)
        finally:
            os.close(marker)
        os.fsync(directory.fd)
        fd = PrivateFileRole.create(staging, UnprivateSidecarFile(), dir_fd=directory.fd)
        try:
            _write_all(fd, raw)
            os.fsync(fd)
            staged = os.fstat(fd)
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
        PrivateFileRole.require(actual, UnprivateSidecarFile())
        os.unlink(intent, dir_fd=directory.fd)
        os.fsync(directory.fd)
        return actual
    except BaseException as error:
        if begun:
            raise SidecarCommitUnknown(
                "Private sidecar commit is uncertain; do not send or retry."
            ) from error
        raise


def create_sidecar_file(path: Path, row_type: type[TypedTable]) -> None:
    """Serialized create-or-verify; an existing empty/damaged file is not repaired."""
    with _locked_directory(path) as directory:
        snapshot = _read_snapshot(directory, path.name)
        if snapshot is not None:
            raw, identity = snapshot
            connection = _connect(raw)
            try:
                VerifiedSnapshots.verify(connection, row_type, path, identity)
                directory.unchanged(path.name, identity)
            finally:
                connection.close()
            return
        connection = _connect(None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _schema(row_type).values():
                connection.execute(statement)
            SnapshotMeta(1, _digest(row_type)).insert(connection)
            _verify_schema(connection, row_type)
            connection.execute("COMMIT")
            _publish(directory, path.name, None, connection.serialize())
        finally:
            connection.close()


@contextmanager
def sidecar_connection(
    path: Path, row_type: type[TypedTable], *, read_only: bool = False
) -> Iterator[sqlite3.Connection]:
    """Verify and use one exact snapshot, committing only on successful scope exit.

    Callers must check rowcount/readback for their own exact inserted identities.
    SQL COMMIT alone is not a durable acknowledgement: the scope must also exit normally.
    """
    with _locked_directory(path) as directory:
        snapshot = _read_snapshot(directory, path.name)
        if snapshot is None:
            raise IdentityConflict("Sidecar store is not installed.")
        raw, identity = snapshot
        connection = _connect(raw)
        try:
            VerifiedSnapshots.verify(connection, row_type, path, identity)
            changes = connection.total_changes
            connection.execute(f"PRAGMA query_only={int(read_only)}")
            connection.set_authorizer(_authorize_sidecar)
            yield connection
            if connection.in_transaction:
                raise IdentityConflict("Sidecar scope left an unfinished transaction.")
            directory.unchanged(path.name, identity)
            if connection.total_changes != changes:
                published = _publish(directory, path.name, identity, connection.serialize())
                VerifiedSnapshots.published(path, row_type, published)
        finally:
            connection.close()


def encode_request(text: str, *, streaming_behavior: type[StreamingInputPolicy] | None = None) -> str:
    """JSON-encode like the pinned native ``_claimNativeInput`` request."""
    return FieldCodec.compact(
        {
            "kind": "prompt",
            "text": text,
            "images": None,
            "streamingBehavior": FieldCodec.encode(streaming_behavior),
            "expandPromptTemplates": True,
            "source": "rpc",
        }, ensure_ascii=False,
    )


def native_request_digest(text: str, *, streaming_behavior: type[StreamingInputPolicy] | None = None) -> str:
    """sha256 over the pinned native request envelope, not the bare text."""
    return hashlib.sha256(
        ("pi-input-request-v1\n" + encode_request(text, streaming_behavior=streaming_behavior)).encode("utf-8")
    ).hexdigest()
