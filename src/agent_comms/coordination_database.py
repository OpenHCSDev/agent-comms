"""Private SQLite connection lifetime and versioned schema installation."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Self

from agent_comms.coordination_errors import IntegrityViolationError, SchemaVersionError
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


class CoordinationStore:
    """Open or initialize the private versioned coordination database."""

    def __init__(self, path: str | os.PathLike[str], *, lock_timeout: float = 5.0) -> None:
        if type(lock_timeout) not in (int, float) or not 0 <= lock_timeout <= 5:
            raise ValueError("coordination lock timeout must be bounded")
        self.path = Path(path)
        self._prepare_private_file()
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=lock_timeout)
        self._connection.create_function(
            "coordination_validate_publication_intent",
            sum(field.init for field in fields(PublicationIntents)),
            PublicationIntents.validate_sql,
            deterministic=True,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(lock_timeout * 1000)}")
        try:
            self._initialize_schema()
            # Rollback journal avoids a post-schema WAL-mode race among fresh openers.
            self._connection.execute("PRAGMA synchronous = FULL")
            self._enforce_private_modes()
        except BaseException:
            self._connection.close()
            raise

    def _prepare_private_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = self.path.lstat()
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
            info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityViolationError("coordination database must be a regular file")
        # NTFS ACLs, not POSIX mode bits, define privacy on Windows. The
        # disposable private execution path is Linux-only; keep the schema
        # usable here without pretending chmod supplies a Windows ACL.
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            os.chmod(self.path, 0o600, follow_symlinks=False)

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
