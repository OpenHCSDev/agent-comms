"""Private SQLite connection lifetime and versioned schema installation."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from abc import ABC, abstractmethod
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Self

from agent_comms.coordination_errors import (
    CoordinationReadUnavailable,
    IntegrityViolationError,
    SchemaVersionError,
)
from agent_comms.coordination_schema import (
    COORDINATION_SCHEMA_VERSION,
    coordinator_schema,
)
from agent_comms.coordination_tables.metadata import SchemaMeta
from agent_comms.coordination_tables.publications import PublicationIntents
from agent_comms.typed_table import (
    SQLiteUserVersion,
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
    """Do not wait when optional presentation or admission cannot acquire SQLite custody."""

    def reader(self, uri: str) -> sqlite3.Connection:
        try:
            return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)
        except sqlite3.OperationalError as error:
            CoordinationReadUnavailable.raise_from(error)

    def writer(
        self, path: str | os.PathLike[str], options: dict[str, object]
    ) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=0, **options)


DEFAULT_STORE_CONTENTION = StoreWait()


class CoordinationStore:
    """Own SQLite read snapshots and the shared writer busy policy."""

    writer_timeout = 5.0

    @staticmethod
    def writer(
        path: str | os.PathLike[str], contention: StoreContention = DEFAULT_STORE_CONTENTION,
        **options: object,
    ) -> sqlite3.Connection:
        """Open a writer through the store family's typed busy behavior."""
        return contention.writer(path, options)

    @staticmethod
    def begin_snapshot(db: sqlite3.Connection) -> None:
        """Acquire one committed read revision before any consumer query."""
        db.execute("BEGIN")
        # BEGIN is lazy. Acquire the snapshot before schema decoders can
        # reclassify BUSY as unsupported data or absent rows.
        _TableName.read(db.execute("SELECT name FROM sqlite_master LIMIT 1"))

    @staticmethod
    def _configure_reader(db: sqlite3.Connection) -> sqlite3.Connection:
        try:
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
        try:
            with closing(CoordinationStore._open_reader(path, contention)) as db:
                yield db
        except sqlite3.OperationalError as error:
            CoordinationReadUnavailable.raise_from(error)

    @staticmethod
    @contextmanager
    def observing(
        path: Path, contention: StoreContention = DEFAULT_STORE_CONTENTION
    ):
        """Own an existing committed read, never initialization or write admission."""
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
            CoordinationReadUnavailable.raise_from(error)

    def __init__(
        self, path: str | os.PathLike[str],
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> None:
        self.path = Path(path)
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
        self._connection.create_function(
            "coordination_validate_publication_intent",
            sum(field.init for field in fields(PublicationIntents)),
            PublicationIntents.validate_sql,
            deterministic=True,
        )
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
        self._connection.execute("BEGIN IMMEDIATE")
        try:
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
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

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
