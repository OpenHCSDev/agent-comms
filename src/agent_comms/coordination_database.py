"""Private coordinator storage and declaration-owned schema installation."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from abc import abstractmethod
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from agent_comms.coordination_errors import IntegrityViolationError
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.typed_table import (
    CarriedWriter,
    ReconciledFile,
    StoreWriter,
)

from .declared_family import DeclaredFamily
from .locked_store import DEFAULT_STORE_CONTENTION, SQLiteFile, StoreContention


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


class CoordinationStore(CarriedWriter, ReconciledFile):
    """The private coordinator database and its declared schema."""

    filename = "coordination.sqlite3"

    @classmethod
    def table_schema(cls) -> type[CoordinatorTable]:
        return CoordinatorTable

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
        if len(header) < 100 or not header.startswith(SQLiteFile.header):
            return InvalidStore("Coordinator has an invalid SQLite header")
        if header[18:20] != b"\x01\x01":
            return InvalidStore("Coordinator is not in rollback-journal mode")
        return ReadableStore(path)

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

    def _initialize_schema(self) -> None:
        self.table_schema().install_schema(self._connection, self.schema_change(self.path))

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

    def writing(self) -> AbstractContextManager[sqlite3.Connection]:
        return StoreWriter.transaction(self, self._connection)

    def _enforce_private_modes(self) -> None:
        if os.name == "nt":
            return  # POSIX no-follow chmod cannot establish an NTFS ACL.
        for candidate in self.files(self.path):
            # SQLite may unlink a journal between observation and chmod.
            with suppress(FileNotFoundError):
                if stat.S_IMODE(candidate.lstat().st_mode) != 0o600:
                    os.chmod(candidate, 0o600, follow_symlinks=False)

    def close(self) -> None:
        self._connection.close()
        self._enforce_private_modes()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
