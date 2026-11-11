"""Private coordinator storage and declaration-owned schema installation."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from agent_comms.coordination.errors import CoordinationReadUnavailable, IntegrityViolationError
from agent_comms.coordination.schema import CoordinatorTable
from agent_comms.execution.child_process import Platform
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.storage.locked_store import FileStore, InputCaptureFile, SQLiteReader
from agent_comms.storage.store_files import DEFAULT_STORE_CONTENTION, StoreContention
from agent_comms.storage.typed_table import (
    CarriedWriter,
    ReconciledFile,
    StoreWriter,
    TableSchema,
)


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
class ReadableStore(FileStore, StoreFile, SQLiteReader):
    """Read-only coordinator view; its lifetime owns the thread-local handles. One view per
    path per process: a view built per read opened a fresh connection and re-parsed the
    coordinator schema on every read, twice per notification pass."""

    held: ClassVar[dict[Path, ReadableStore]] = {}

    @classmethod
    def at(cls, path: Path) -> ReadableStore:
        return cls.held.setdefault(path, cls(path))

    def _capture(self, consumer, *arguments):
        with self.reader_snapshot() as connection:
            yield consumer.capture(connection, *arguments)


class CoordinationStore(FileStore, CarriedWriter, ReconciledFile, InputCaptureFile):
    """The private coordinator database and its declared schema."""

    filename = "coordination.sqlite3"

    @classmethod
    def table_schema(cls) -> type[CoordinatorTable]:
        return CoordinatorTable

    @classmethod
    def schema_at(cls, path: Path) -> type[TableSchema]:
        from agent_comms.registry.registry_store import RegistryStore

        return RegistryStore.at(path.parent).coordinator_schema()

    @classmethod
    def observed(cls, path: Path) -> StoreFile:
        """Classify through SQLite without dropping another connection's process locks.

        Both rollback and WAL snapshots are readable. SQLite may maintain its own
        WAL companions; observation never changes stored rows, installs a schema,
        chmods a file, or permits recovery to write through a read-only connection.
        """
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return InvalidStore("Coordinator is not a regular file")
            if not info.st_size:
                return InvalidStore("Coordinator is empty")
            store = ReadableStore.at(path)
            with CoordinationReadUnavailable.checking(), store.reader_snapshot():
                pass
        except FileNotFoundError:
            return MissingStore()
        except PermissionError as error:
            return InvalidStore(str(error))
        except sqlite3.DatabaseError as error:
            return InvalidStore(str(error))
        return store

    def __init__(
        self,
        path: str | os.PathLike[str],
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> None:
        super().__init__(Path(path))
        self._prepare_private_file()
        with CoordinationReadUnavailable.checking():
            self._connection = self._open_connection(contention)
            try:
                self.configure_connection(self._connection)
                self._initialize_schema()
                self._enforce_private_modes()
            except BaseException:
                self._connection.close()
                raise

    def _open_connection(self, contention: StoreContention) -> sqlite3.Connection:
        return self.writer(self.path, contention, isolation_level=None)

    def _initialize_schema(self) -> None:
        """The root's own composition of this file decides its schema and its strays."""
        self.schema_at(self.path).install_schema(self._connection, self.schema_change(self.path))

    @classmethod
    def configure_connection(cls, connection: sqlite3.Connection) -> None:
        """Ordinary opening and selected loading use the same row-owned SQL behavior."""
        for row_type in CoordinatorTable.declarations():
            row_type.configure_connection(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # Rollback journal avoids a post-schema WAL-mode race among fresh openers.
        connection.execute("PRAGMA synchronous = FULL")

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
                    Platform.current().publish_new_file(staging, self.path)
            finally:
                Path(staging).unlink(missing_ok=True)
        self._private_file()
        Platform.current().make_private(self.path, 0o600)

    def _private_file(self) -> os.stat_result:
        try:
            info = self.path.lstat()
        except FileNotFoundError as error:
            raise IntegrityViolationError("coordination database is missing") from error
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityViolationError("coordination database must be a regular file")
        return info

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        with CoordinationReadUnavailable.checking(), StoreWriter.transaction(self, self._connection) as db:
            yield db

    def _enforce_private_modes(self) -> None:
        for candidate in self.files(self.path):
            Platform.current().make_private(candidate, 0o600)

    def close(self) -> None:
        self._connection.close()
        self._enforce_private_modes()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
