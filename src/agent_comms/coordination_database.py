"""Private SQLite connection lifetime and versioned schema installation."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .declared_family import DeclaredFamily

from agent_comms.coordination_errors import (
    CoordinationReadUnavailable,
    IntegrityViolationError,
    SchemaVersionError,
)
from agent_comms.coordination_schema import (
    COORDINATION_SCHEMA_VERSION,
    CoordinatorTable,
    coordinator_schema,
)
from agent_comms.coordination_tables.metadata import SchemaMeta
from agent_comms.typed_table import (
    SQLiteUserVersion,
    StoreWriter,
    TypedRow,
)


@dataclass(frozen=True)
class _TableName(TypedRow):
    name: str


class StoreContention(ABC):
    """Typed SQLite busy behavior shared by snapshots and writers."""

    @abstractmethod
    def reader(self, uri: str) -> sqlite3.Connection: ...

    @abstractmethod
    def writer(
        self, path: str | os.PathLike[str], options: dict[str, object]
    ) -> sqlite3.Connection: ...


@dataclass(frozen=True)
class StoreWait(StoreContention):
    def reader(self, uri: str) -> sqlite3.Connection:
        return sqlite3.connect(uri, uri=True, isolation_level=None)

    def writer(
        self, path: str | os.PathLike[str], options: dict[str, object]
    ) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=CoordinationStore.writer_timeout, **options)


@dataclass(frozen=True)
class StoreBusy(StoreContention):
    """Do not wait when optional display or input work cannot acquire a SQLite lock."""

    def reader(self, uri: str) -> sqlite3.Connection:
        with CoordinationReadUnavailable.checking():
            return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)

    def writer(
        self, path: str | os.PathLike[str], options: dict[str, object]
    ) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=0, **options)


DEFAULT_STORE_CONTENTION = StoreWait()


class StoreFile(DeclaredFamily):
    """One decoded file state, with the consumer's result inside its read lifetime."""

    @property
    def failure_reason(self) -> str:
        """Public reason spelling; an invalid file's diagnostic details stay private."""
        return self.declared_name

    @contextmanager
    def observing(self, consumer, *arguments):
        yield from self._capture(consumer, *arguments)

    @abstractmethod
    def _capture(self, consumer, *arguments): ...


@dataclass(frozen=True)
class MissingStore(StoreFile, declared_name="missing"):
    def _capture(self, consumer, *arguments):
        yield consumer.missing_store(self, *arguments)


@dataclass(frozen=True)
class InvalidStore(StoreFile):
    reason: str

    def _capture(self, consumer, *arguments):
        yield consumer.invalid_store(self, *arguments)


@dataclass(frozen=True)
class ReadableStore(StoreFile):
    path: Path

    def _capture(self, consumer, *arguments):
        with CoordinationStore.observing(self.path) as connection:
            yield consumer.capture(connection, *arguments)


class CoordinationStore(StoreWriter):
    """Own SQLite read snapshots and the shared writer busy policy."""

    filename = "coordination.sqlite3"
    writer_timeout = 5.0
    header = b"SQLite format 3\0"

    @staticmethod
    def files(path: Path) -> tuple[Path, ...]:
        """The database and the companion files SQLite keeps beside it: together, one store."""
        companions = ("-wal", "-shm", "-journal")
        return (path, *(path.with_name(path.name + suffix) for suffix in companions))

    @classmethod
    def is_database(cls, path: Path) -> bool:
        with path.open("rb") as stream:
            return stream.read(len(cls.header)) == cls.header

    @classmethod
    def digest(cls, path: Path) -> str:
        """The store's version, schema and rows as SQLite reads them, whichever of its
        files hold them."""
        with closing(cls.read_snapshot(path)) as db:
            content = hashlib.sha256(str(db.execute("PRAGMA user_version").fetchone()[0]).encode())
            for statement in db.iterdump():
                content.update(statement.encode())
            return content.hexdigest()

    @staticmethod
    def writer(
        path: str | os.PathLike[str], contention: StoreContention = DEFAULT_STORE_CONTENTION,
        **options: object,
    ) -> sqlite3.Connection:
        """Open a writer through the store family's typed busy behavior."""
        return CoordinationStore.identifiers_only(contention.writer(path, options))

    @staticmethod
    def identifiers_only(db: sqlite3.Connection) -> sqlite3.Connection:
        """A double-quoted name is an identifier, never a string: a missing column fails
        as missing instead of reading back as a literal named after it."""
        for option in (sqlite3.SQLITE_DBCONFIG_DQS_DML, sqlite3.SQLITE_DBCONFIG_DQS_DDL):
            db.setconfig(option, False)
        return db

    @staticmethod
    def begin_snapshot(db: sqlite3.Connection) -> None:
        """Acquire one committed read revision before any consumer query."""
        db.execute("BEGIN")
        # BEGIN is lazy. Acquire the snapshot before schema decoders can
        # reclassify BUSY as unsupported data or absent rows.
        _TableName.read(db.execute("SELECT name FROM sqlite_master LIMIT 1"))

    @classmethod
    def _configure_reader(cls, db: sqlite3.Connection) -> sqlite3.Connection:
        try:
            cls.identifiers_only(db)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA foreign_keys=ON")
            return db
        except BaseException:
            db.close()
            raise

    @classmethod
    def _open_reader(
        cls, path: Path, contention: StoreContention
    ) -> sqlite3.Connection:
        return cls._configure_reader(
            contention.reader(path.resolve().as_uri() + "?mode=ro")
        )

    @classmethod
    def _acquire_snapshot(cls, db: sqlite3.Connection) -> sqlite3.Connection:
        try:
            cls.begin_snapshot(db)
            return db
        except BaseException:
            db.close()
            raise

    @classmethod
    def read_snapshot(
        cls, path: Path, contention: StoreContention = DEFAULT_STORE_CONTENTION
    ) -> sqlite3.Connection:
        """Open and acquire one consistent read-only snapshot with SQLite's busy handler."""
        return cls._acquire_snapshot(cls._open_reader(path, contention))

    @classmethod
    def recovering_snapshot(cls, path: Path) -> sqlite3.Connection:
        """Acquire evidence after SQLite is allowed to recover an existing hot journal."""
        uri = path.resolve().as_uri() + "?mode=rw"
        return cls._acquire_snapshot(cls._configure_reader(cls.writer(uri, uri=True)))

    @staticmethod
    @contextmanager
    def reading(
        path: Path, contention: StoreContention = DEFAULT_STORE_CONTENTION
    ):
        """Own a configured read connection whose consumers bound each snapshot."""
        with CoordinationReadUnavailable.checking():
            with closing(CoordinationStore._open_reader(path, contention)) as db:
                yield db

    @staticmethod
    def observed(path: Path) -> StoreFile:
        """Never let a read-only SQLite open create a WAL shared-memory sidecar.

        WAL-mode SQLite can create -shm even when opened with mode=ro. Refuse such a
        database *before* opening SQLite; rollback-journal mode is the current
        coordinator configuration. Do not create directories, chmod, or recover a
        hot journal. A concurrent WAL conversion is outside this schema contract.
        """
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return InvalidStore("Coordinator is not a regular file")
            with path.open("rb") as stream:
                header = stream.read(100)
        except FileNotFoundError:
            return MissingStore()
        except PermissionError as error:
            return InvalidStore(str(error))
        if len(header) < 100 or not header.startswith(CoordinationStore.header):
            return InvalidStore("Coordinator has an invalid SQLite header")
        if header[18:20] != b"\x01\x01":
            return InvalidStore("Coordinator is not in rollback-journal mode")
        return ReadableStore(path)

    @staticmethod
    @contextmanager
    def observing(
        path: Path, contention: StoreContention = DEFAULT_STORE_CONTENTION
    ):
        """Read existing committed data without initializing storage or permitting writes."""
        with CoordinationReadUnavailable.checking():
            try:
                with closing(CoordinationStore.read_snapshot(path, contention)) as db:
                    try:
                        yield db
                    finally:
                        if db.in_transaction:
                            db.execute("ROLLBACK")
            except sqlite3.OperationalError as error:
                error.add_note(
                    f"SQLite observation store: {path}; "
                    f"SQLite error: {error.sqlite_errorname} ({error.sqlite_errorcode})"
                )
                raise

    def __init__(
        self, path: str | os.PathLike[str],
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> None:
        super().__init__(Path(path))
        self._prepare_private_file()
        self._connection = self._open_connection(contention)
        try:
            self._configure_connection()
            self._initialize_schema()
            self._enforce_private_modes()
        except BaseException:
            self._connection.close()
            raise

    def _open_connection(self, contention: StoreContention) -> sqlite3.Connection:
        return self.writer(self.path, contention, isolation_level=None)

    def _configure_connection(self) -> None:
        for row_type in CoordinatorTable.declarations():
            row_type.configure_connection(self._connection)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        # Rollback journal avoids a post-schema WAL-mode race among fresh openers.
        self._connection.execute("PRAGMA synchronous = FULL")

    def _prepare_private_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.lstat()
        except FileNotFoundError:
            # Closing *any* fd for an already-published SQLite inode can release
            # this process's classic fcntl locks, even those held by another
            # connection.  Close the private staging fd before publishing it.
            descriptor, staging = tempfile.mkstemp(prefix=".coord-init-", dir=self.path.parent)
            try:
                os.close(descriptor)
                # Another initializer may win; never overwrite its inode.
                with suppress(FileExistsError):
                    os.link(staging, self.path, follow_symlinks=False)
            finally:
                os.unlink(staging)
        info = self._private_file()
        # NTFS ACLs, not POSIX mode bits, define privacy on Windows. The
        # disposable private execution path is Linux-only; keep the schema
        # usable here without pretending chmod supplies a Windows ACL.
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            os.chmod(self.path, 0o600, follow_symlinks=False)

    def _private_file(self) -> os.stat_result:
        try:
            info = self.path.lstat()
        except FileNotFoundError as error:
            raise IntegrityViolationError("coordination database is missing") from error
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityViolationError("coordination database must be a regular file")
        return info

    def _initialize_schema(self) -> None:
        # Existing immutable metadata needs only a consistent read. Opening an
        # ordinary reader must never become a writer awaiting other readers'
        # release, especially before the wire -> registry -> SQLite lock order.
        self._connection.execute("BEGIN")
        try:
            version = self.schema_version
            if not self._empty_schema(version):
                SchemaMeta.require_current(self._connection, version)
                return
        finally:
            self._connection.execute("ROLLBACK")

        # Only installation owns a write transaction. Recheck after taking it:
        # another initializer may have installed the schema since the read.
        # SQLite's executescript() implicitly commits an existing transaction; execute
        # complete trigger-aware statements individually under this one lock.
        with self.writing():
            version = self.schema_version
            if self._empty_schema(version):
                statement = ""
                for line in coordinator_schema().splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise SchemaVersionError("coordination schema has an incomplete statement")
                SchemaMeta.current().insert(self._connection)
                self._connection.execute(f"PRAGMA user_version = {COORDINATION_SCHEMA_VERSION}")
            else:
                SchemaMeta.require_current(self._connection, version)

    def writing(self) -> AbstractContextManager[sqlite3.Connection]:
        return StoreWriter.transaction(self, self._connection)

    def _empty_schema(self, version: int) -> bool:
        return version == 0 and not _TableName.read(
            self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        )

    def _enforce_private_modes(self) -> None:
        if os.name == "nt":
            return  # POSIX no-follow chmod cannot establish an NTFS ACL.
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            # SQLite may unlink a journal between observation and chmod.
            with suppress(FileNotFoundError):
                if stat.S_IMODE(candidate.lstat().st_mode) != 0o600:
                    os.chmod(candidate, 0o600, follow_symlinks=False)

    @property
    def schema_version(self) -> int:
        (version,) = SQLiteUserVersion.read(self._connection.execute("PRAGMA user_version"))
        return version.user_version

    def close(self) -> None:
        self._connection.close()
        self._enforce_private_modes()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
