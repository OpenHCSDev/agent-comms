"""Declared file behavior, owned SQLite connections, and durable JSON replacement."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from pathlib import Path
from pkgutil import walk_packages
from typing import ClassVar, Generic, Self, TypeVar

from pydantic import JsonValue

from .child_process import Platform
from .coordination_errors import CoordinationReadUnavailable
from .field_codec import FieldCodec, MalformedRecord
from .private_path import PrivateFileRole
from .store_files import (
    AtomicFile,
    SharedLock,
    StoreLock,
    StoreLockContention,
    StoreLockFile,
    StoreLockOwner,
    StoreOwner,
    _store_lock,
    is_store_lock,
    store_lock_path,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DocumentChange[D, R]:
    """A document's successor and the answer its change gives the caller."""

    document: D
    result: R

    @classmethod
    def to(cls, document: D) -> DocumentChange[D, D]:
        """A change whose answer is the successor itself."""
        return cls(document, document)


class RootFile:
    """A declared bus-file location, independent of its value or I/O constructor."""

    __slots__ = ()
    filename: ClassVar[str]
    root_directory: ClassVar[str] = "."

    def __init_subclass__(cls, **kwargs: object) -> None:
        """A declared file is either retained across installations or rewritten by its
        running owner; a declaration that is neither has no install behaviour at all."""
        super().__init_subclass__(**kwargs)
        if "filename" in vars(cls) and not issubclass(cls, (DurableFile, RuntimeFile)):
            raise TypeError(f"{cls.__qualname__} declares {cls.filename!r} but is neither "
                            "a DurableFile nor a RuntimeFile")

    @classmethod
    def path_in(cls, root: Path | str, **parts: str) -> Path:
        return Path(root) / cls.filename.format(**parts)

    @classmethod
    def directory_in(cls, root: Path | str) -> Path:
        """The directory this declaration's files live in under the bus root ``root``;
        ``bus_root`` is its inverse."""
        return Path(root) / cls.root_directory

    @classmethod
    def bus_root(cls, path: Path) -> Path:
        """The bus root a file of this declaration at ``path`` lives under: one parent per
        directory its declared location names, the filename's own directories included."""
        return path.parents[len((Path(cls.root_directory) / cls.filename).parts) - 1]

    @classmethod
    def member_paths(cls, path: Path) -> frozenset[Path]:
        """The resources composed onto one declared primary file."""
        return frozenset((path, store_lock_path(path)))

    @classmethod
    def transient_paths(cls, path: Path) -> frozenset[Path]:
        return frozenset((store_lock_path(path),))

    @classmethod
    def snapshot_members(
        cls, path: Path, observed: Mapping[Path, os.stat_result],
    ) -> frozenset[Path]:
        """Content changes count; lock and coordination files are not content."""
        return frozenset((path,))

    @classmethod
    def files_in(cls, root: Path) -> frozenset[Path]:
        return cls.member_paths(cls.path_in(root))

    @classmethod
    def matching_in(cls, root: Path) -> frozenset[Path]:
        """The declared files themselves that exist, without their locks."""
        path = cls.path_in(root)
        return frozenset((path,)) if path.is_file() else frozenset()


    @staticmethod
    def copy_file(source: str | Path, target: str | Path) -> Path:
        return Path(shutil.copy2(source, target))

    @classmethod
    def copy_group(cls, source: Path, target: Path) -> frozenset[Path]:
        """Copy this owner's file cut and name the source files included. A file no writer
        locks is copied as it is: locking it would create a lock inode beside it."""
        cls.copy_file(source, target)
        return frozenset((source,))

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def files(path: Path) -> tuple[Path, ...]:
        return (path,)

    @staticmethod
    @cache
    def load_declarations() -> None:
        """Import every module of the package once per process, so ``declarations_in`` walks
        the whole family and not only the owners some caller happened to import."""
        package = import_module(__package__)
        for module in walk_packages(package.__path__, f"{package.__name__}."):
            if not module.name.startswith(f"{package.__name__}.skills"):
                import_module(module.name)

    @classmethod
    def declarations_in(cls, root: Path) -> Iterator[tuple[type[Self], Path]]:
        """Each declaring owner and its directory, once across composed capabilities."""
        RootFile.load_declarations()
        pending: list[type[Self]] = [cls]
        visited: set[type[Self]] = set()
        while pending:
            family = pending.pop()
            if family in visited:
                continue
            visited.add(family)
            pending.extend(family.__subclasses__())
            if "filename" in vars(family):
                yield family, family.directory_in(root)

    @classmethod
    def existing_in(cls, root: Path) -> dict[Path, type[Self]]:
        """Every existing file a declared owner names, with that owner, never a probe of
        listed files: an undeclared file (a native writer's transient claim, a lock) has no
        owner, so nothing that walks a bus by its declarations meets it."""
        return {path: owner for owner, directory in cls.declarations_in(root)
                for path in owner.matching_in(directory)}

    @classmethod
    def archived_roots(cls, directory: Path) -> tuple[Path, ...]:
        """Whole bus roots this declaration keeps as immutable copies under ``directory``."""
        return ()

    @classmethod
    def primaries_in(cls, root: Path) -> dict[Path, type[Self]]:
        """Existing declared files under ``root`` and every root archived in it, each with
        its own owner, so an archived store's companions stay with that store."""
        primaries = cls.existing_in(root)
        for owner, directory in RootFile.declarations_in(root):
            for archived in owner.archived_roots(directory):
                primaries.update(cls.primaries_in(archived))
        return primaries

    @classmethod
    def declaring_owner(cls) -> type[RootFile]:
        """A locking store can inherit its filename from a location-only declaration."""
        if issubclass(cls, StoreLockFile):
            return cls
        stores = tuple(
            owner
            for owner in StoreLockFile.members_with(cls)
            if issubclass(owner, RootFile)
            and owner.filename == cls.filename
            and owner.root_directory == cls.root_directory
        )
        if not stores:
            return cls
        (owner,) = stores
        return owner

    @classmethod
    def owned_files(
        cls, root: Path, paths: Collection[Path],
    ) -> dict[DeclaredFile | UndeclaredFile, type[RootFile]]:
        """Resolve primaries once; companions belong to their primary's declaration.

        Missing selected files still identify an absent resource to restore. An
        undeclared existing file acquires content behavior without inventing a
        store declaration for it.
        """
        primaries = cls.primaries_in(root)
        declaring = {
            owner: owner.declaring_owner() for owner, _ in cls.declarations_in(root)
        }
        declarations = {
            member: declaring[owner]
            for owner, directory in cls.declarations_in(root)
            for member in owner.files_in(directory)
        }
        declarations.update((path, declaring[owner]) for path, owner in primaries.items())
        selected = frozenset(paths)
        owners: dict[Path, type[RootFile]] = {
            path: owner for path, owner in primaries.items() if path in selected
        }
        companions = {member for path, owner in owners.items()
                      for member in (*owner.files(path), *owner.transient_paths(path))
                      if member != path}
        owners.update((path, RootFile)
                      for path in selected - owners.keys() - companions)
        companions.update(member for path, owner in owners.items()
                          for member in (*owner.files(path), *owner.transient_paths(path))
                          if member != path)
        # A lock is never content, even after a carry deletes the store it locked.
        companions.update(path for path in selected if is_store_lock(path))
        return {
            (DeclaredFile(declarations[path], path.relative_to(root)) if path in declarations
             else UndeclaredFile(path.relative_to(root))): owner
            for path, owner in owners.items() if path not in companions
        }

    @classmethod
    def paths_in(cls, root: Path) -> frozenset[Path]:
        return frozenset(
            path for owner, directory in cls.declarations_in(root)
            for path in owner.files_in(directory)
        )


class JsonDocument(RootFile, ABC, Generic[T]):
    """Declared JSON codec and atomic file I/O; LockedStore owns transactions."""

    path: Path
    json_indent: ClassVar[int | None] = None
    json_sort_keys: ClassVar[bool] = False
    json_suffix: ClassVar[str] = ""

    record_type: ClassVar[type]
    """The declaration decoded by FieldCodec, including container types."""

    @abstractmethod
    def empty(self) -> T:
        """Return a fresh value for a missing document."""

    def _read_unlocked(self) -> T:
        return self._read_using(self._decode)

    def _read_using(self, decode: Callable[[JsonValue], T]) -> T:
        try:
            return decode(FieldCodec.parse(self.path.read_bytes()))
        except FileNotFoundError:
            return self.empty()
        except (OSError, MalformedRecord) as error:
            return self._unreadable(error)

    def _unreadable(self, error: Exception) -> T:
        """Strict by default; optional owners may declare a fail-closed value."""
        raise error

    def _decode(self, data: JsonValue) -> T:
        """Decode the current declaration at the storage boundary."""
        return FieldCodec.decode_document(self.record_type, data)

    def _encode(self, value: T) -> JsonValue:
        """Owners with an established external shape may project that encoding."""
        return FieldCodec.encode_document(value, self.record_type)

    def read(self) -> T:
        return self._read_unlocked()

    def _publish_unlocked(self, value: T) -> None:
        self._write_unlocked(
            json.dumps(self._encode(value), indent=self.json_indent, sort_keys=self.json_sort_keys)
            + self.json_suffix
        )

    def _write_unlocked(self, text: str) -> None:
        """Stage before publication; retain the old inode until directory sync.

        A reported staging/publication/fsync failure restores the old bytes and
        mode while the exclusive lock is held. A second failure during rollback
        is propagated too; no filesystem can promise recovery from arbitrary
        repeated I/O failure. Process death after replace has the usual atomic
        old-or-new semantics, not a multi-file transaction or recovery journal.
        """
        fd, staged = StoreOwner.stage_file(self.path)
        os.close(fd)
        staged.unlink()
        backup = StoreOwner.rollback_file(staged)
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            os.link(self.path, backup)
        except FileNotFoundError:
            mode = PrivateFileRole.permissions
        try:
            try:
                AtomicFile(self.path, mode).write_text(text)
                backup.unlink(missing_ok=True)
            except BaseException:
                if backup.exists():
                    if not self.path.exists() or not self.path.samefile(backup):
                        os.replace(backup, self.path)
                        Platform.current().sync_directory(self.path.parent)
                else:
                    self.path.unlink(missing_ok=True)
                    Platform.current().sync_directory(self.path.parent)
                raise
        finally:
            # A failed restore leaves the last original inode for recovery.
            if backup.exists() and self.path.exists() and self.path.samefile(backup):
                backup.unlink()



@dataclass(frozen=True)
class DeclaredFile(RootFile):
    """A declared store and one retained file path identify a resource."""

    owner: type[RootFile]
    path: Path
    declared: ClassVar[bool] = True

    def takes_store_lock(self) -> bool:
        return issubclass(self.owner, StoreLockFile)


@dataclass(frozen=True)
class UndeclaredFile(RootFile):
    """A physical file without a stored declaration, still identified by its path."""

    path: Path
    declared: ClassVar[bool] = False
    """No declaration answers whether its writers take a lock, so a lock beside it is unknown."""

    def takes_store_lock(self) -> bool:
        return False


class LockedFile(RootFile, StoreLockOwner, declared_name=None):
    """A declared file whose writers hold its store lock, so its copy shares that lock."""

    __slots__ = ()

    @classmethod
    def copy_group(cls, source: Path, target: Path) -> frozenset[Path]:
        with _store_lock(source, mode=SharedLock()):
            return super().copy_group(source, target)


class FileStore(RootFile, ABC):
    """Construction belongs only to owners whose initializer takes a file."""

    __slots__ = ()

    @abstractmethod
    def __init__(self, path: Path, *args, **kwargs) -> None: ...

    @classmethod
    def at(cls, root: Path | str, *args, **kwargs) -> Self:
        return cls(cls.path_in(root), *args, **kwargs)


class PatternFile(RootFile):
    """Enumerate an owner's declared parameterized names, never the bus directory."""

    __slots__ = ()

    @classmethod
    def pattern(cls) -> Path:
        return Path(cls.filename.format_map(defaultdict(lambda: "*")))

    @classmethod
    def matching_in(cls, root: Path) -> frozenset[Path]:
        """The declared files themselves, without their locks."""
        return frozenset(path for path in root.glob(str(cls.pattern())) if path.is_file())

    @classmethod
    def files_in(cls, root: Path) -> frozenset[Path]:
        return frozenset(member for path in cls.matching_in(root)
                         for member in cls.member_paths(path)) | frozenset(
            path for path in root.glob(str(store_lock_path(cls.pattern()))) if path.is_file())


class DurableFile(RootFile):
    """Bus state retained across installations, with conversion owned by its store."""

    __slots__ = ()


class RuntimeFile(RootFile):
    """A bus file no carry writes: running owners rewrite it at any time, maintenance included."""

    __slots__ = ()


class BusFile(DurableFile):
    """Declared data and lifecycle files that rule out a fresh bus installation."""

    @classmethod
    def fresh(cls, root: Path) -> bool:
        return not any(
            path.exists() or path.is_symlink()
            for owner, directory in cls.declarations_in(root)
            for path in (owner.path_in(directory),)
        )


class WireWakeFile(RootFile):
    """Declared commit signals wake idle readers, unlike lock-file churn."""

    __slots__ = ()

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        return (cls.path_in(root),)

    @classmethod
    def watched_paths_in(cls, root: Path) -> frozenset[Path]:
        return frozenset(
            path
            for owner, directory in cls.declarations_in(root)
            for path in owner.source_paths(directory)
        )


class RegistryFile(DurableFile, WireWakeFile):
    """Registry location usable by record types without importing their decoder."""

    __slots__ = ()
    filename = "registry.json"

    @classmethod
    def copy_group(cls, source: Path, target: Path) -> frozenset[Path]:
        from .registry_store import RegistryStore

        registry = RegistryStore(source)
        with registry.locked(mode=SharedLock()):
            copied = frozenset(path for path in registry.cut_paths if path.exists())
            for path in copied:
                destination = target.parent / path.relative_to(source.parent)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cls.copy_file(path, destination)
        return copied


class RetiredCommanderFile(DurableFile):
    """The retired commander file that tools/cutover/carry_commander_project.py consumes.
    The practice copy holds declared files only, so an undeclared input made the practice
    carry find nothing while the live carry deleted it. Deleted with that carry."""

    __slots__ = ()
    filename = "commander.json"


class StoreContention(ABC):
    """Typed SQLite busy behavior shared by snapshots and writers."""

    @property
    @abstractmethod
    def timeout(self) -> float: ...

    def reader(self, uri: str) -> sqlite3.Connection:
        return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=self.timeout)

    def writer(
        self, path: str | os.PathLike[str], options: dict[str, object]
    ) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=self.timeout, **options)

    def lock_budget(self) -> StoreLockContention:
        """The same wait spent on the store's file locks: one fresh budget per operation."""
        return StoreLockContention(self.timeout)


@dataclass(frozen=True)
class StoreWait(StoreContention):
    timeout: ClassVar[float] = 5.0


@dataclass(frozen=True)
class StoreBusy(StoreContention):
    """Do not wait when optional display or input work cannot acquire a SQLite lock."""

    timeout: ClassVar[float] = 0.0

    def reader(self, uri: str) -> sqlite3.Connection:
        with CoordinationReadUnavailable.checking():
            return super().reader(uri)


DEFAULT_STORE_CONTENTION = StoreWait()


class SQLiteFile(RootFile):
    """SQLite connections, snapshots, copies and companions share one file owner."""

    __slots__ = ()
    wal_suffix: ClassVar[str] = "-wal"
    transient_suffixes: ClassVar[tuple[str, ...]] = ("-shm", "-journal")

    @classmethod
    def writer(
        cls, path: str | os.PathLike[str], contention: StoreContention = DEFAULT_STORE_CONTENTION,
        **options: object,
    ) -> sqlite3.Connection:
        """Open a writer through the store family's typed busy behavior."""
        return cls.identifiers_only(contention.writer(path, options))

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
        db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()

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

    @classmethod
    @contextmanager
    def observing(
        cls, path: Path, contention: StoreContention = DEFAULT_STORE_CONTENTION
    ):
        """Read existing committed data without initializing storage or permitting writes."""
        with CoordinationReadUnavailable.checking():
            try:
                with closing(cls.read_snapshot(path, contention)) as db:
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


    @classmethod
    def copy_file(cls, source: str | Path, target: str | Path) -> Path:
        """Copy a committed SQLite snapshot, excluding uncommitted companion bytes."""
        source, target = Path(source), Path(target)
        with (closing(cls.read_snapshot(source)) as original,
              closing(cls.writer(target)) as copied):
            original.backup(copied)
        shutil.copystat(source, target)
        return target

    @classmethod
    def copy_group(cls, source: Path, target: Path) -> frozenset[Path]:
        """SQLite's snapshot owns the whole cut without a file lock spanning the copy."""
        cls.copy_file(source, target)
        return frozenset((source,))

    @classmethod
    def digest(cls, path: Path) -> str:
        """Hash a consistent SQLite schema and row dump, including committed WAL data."""
        with closing(cls.read_snapshot(path)) as database:
            content = hashlib.sha256()
            for statement in database.iterdump():
                content.update(statement.encode())
            return content.hexdigest()

    @classmethod
    def files(cls, path: Path) -> tuple[Path, ...]:
        return (path, cls.wal_path(path),
                *(path.with_name(path.name + suffix) for suffix in cls.transient_suffixes))

    @classmethod
    def wal_path(cls, path: Path) -> Path:
        return path.with_name(path.name + cls.wal_suffix)

    @classmethod
    def transient_paths(cls, path: Path) -> frozenset[Path]:
        return super().transient_paths(path) | frozenset((
            *(set(cls.files(path)) - {path, cls.wal_path(path)}), StoreOwner.snapshot_lock(path),
        ))

    @classmethod
    def snapshot_members(
        cls, path: Path, observed: Mapping[Path, os.stat_result],
    ) -> frozenset[Path]:
        wal = cls.wal_path(path)
        # A read can create an empty WAL; only a nonempty WAL holds content.
        return super().snapshot_members(path, observed) | frozenset(
            (wal,) if wal in observed and observed[wal].st_size else ()
        )

    @classmethod
    def member_paths(cls, path: Path) -> frozenset[Path]:
        return super().member_paths(path) | frozenset((
            *cls.files(path), StoreOwner.snapshot_lock(path), StoreOwner.snapshot_intent(path),
        ))

    @classmethod
    def _begin_transaction(cls, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @classmethod
    @contextmanager
    def write_transaction(cls, connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """The one write transaction: committed when the block returns, rolled back when it raises."""
        cls._begin_transaction(connection)
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


class StoreCommittedError(OSError):
    """COMMIT succeeded; change signalling or connection retirement failed."""


class PublishedSQLiteFile(SQLiteFile):
    """Publish only completed row changes, independently of database attachment."""

    __slots__ = ()

    @staticmethod
    def _source_paths(path: Path) -> tuple[Path, ...]:
        return (path.with_suffix(".changed"),)

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        return cls._source_paths(cls.path_in(root))

    @classmethod
    def member_paths(cls, path: Path) -> frozenset[Path]:
        return super().member_paths(path) | frozenset(cls._source_paths(path))

    @classmethod
    @contextmanager
    def publishing_changes(
        cls, connection: sqlite3.Connection, path: Path,
    ) -> Iterator[None]:
        before = connection.total_changes
        yield
        if connection.total_changes != before:
            cls.publish_change(path)

    @classmethod
    def publish_change(cls, path: Path) -> None:
        """Announce a completed mutation or replacement of this owner's store."""
        marker, = cls._source_paths(path)
        try:
            marker.write_bytes(b"")
        except OSError as error:
            raise StoreCommittedError(
                f"Committed {path}, but change notification failed: {error}"
            ) from error


@dataclass(frozen=True, slots=True)
class LockedStore(FileStore, LockedFile, JsonDocument[T], declared_name=None):
    """Ordered document transactions; callbacks never mutate or reenter."""

    path: Path
    @contextmanager
    def reading(self, *sources) -> Iterator[T]:
        """Retain dependent source owners before this document's shared lock."""
        with StoreLock.ordered(dict.fromkeys((*sources, self), SharedLock())):
            yield self._read_unlocked()

    def read(self) -> T:
        with self.reading() as value:
            return value

    def update[R](self, change: Callable[[T], DocumentChange[T, R]]) -> R:
        with self.locked():
            original = self._read_unlocked()
            changed = change(original)
            if changed.document is not original:
                self._publish_unlocked(changed.document)
            return changed.result

    def replace(self, value: T) -> None:
        """Publish a complete owned snapshot without reading the discarded one.

        Use update for merges. Derived projections must hold their source lock
        through capture and replacement; this document lock alone is not source
        authority or a cross-document transaction.
        """
        with self.locked():
            self._publish_unlocked(value)
