"""Declared file behavior, owned SQLite connections, and durable JSON replacement."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from pathlib import Path
from pkgutil import walk_packages
from typing import TYPE_CHECKING, ClassVar, Generic, Self, TypeVar

from pydantic import JsonValue

from .child_process import Platform
from .coordination_errors import CoordinationReadUnavailable
from .field_codec import FieldCodec, MalformedRecord
from .errors import RelationViolationError
from .private_path import PrivateFileRole
from .store_files import (
    AtomicFile,
    DEFAULT_ISOLATION,
    DEFAULT_STORE_CONTENTION,
    SharedLock,
    SqliteStore,
    StoreContention,
    StoreLock,
    StoreLockContention,
    StoreLockFile,
    StoreLockOwner,
    StoreOwner,
    _store_lock,
    is_store_lock,
    store_lock_path,
)

if TYPE_CHECKING:
    from sqlite3 import _IsolationLevel

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
    def content_members(cls, path: Path) -> frozenset[Path]:
        return frozenset((path,))

    @staticmethod
    def relocated(source: Path, target: Path, member: Path) -> Path:
        return target if member == source else target.parent / member.relative_to(source.parent)

    @classmethod
    def copy_group(cls, source: Path, target: Path) -> frozenset[Path]:
        """Copy exactly the content members this declaration retains."""
        members = cls.content_members(source)
        for member in members:
            cls.copy_file(member, cls.relocated(source, target, member))
        return members

    @classmethod
    def content_files(cls, path: Path, name: Path) -> dict[Path, Path]:
        return {cls.relocated(path, name, member): member
                for member in cls.content_members(path) if member.is_file()}

    @classmethod
    def capture_file(cls, path: Path, name: Path, directory: Path) -> dict[Path, str]:
        if not path.exists():
            return {}
        saved = {}
        with cls.writers_drained(path):
            for retained, source in cls.content_files(path, name).items():
                target = directory / retained
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cls.copy_file(source, target)
                Platform.current().sync_file(target)
                saved[retained] = cls.digest(target)
                Platform.current().sync_directory(target.parent)
        return saved

    @classmethod
    def file_matches(cls, path: Path, name: Path, saved: Mapping[Path, str]) -> bool:
        current = cls.content_files(path, name)
        retained = {cls.relocated(path, name, member) for member in cls.files(path)} & saved.keys()
        return (current.keys() == retained
                and all(cls.digest(source) == saved[key] for key, source in current.items()))

    @staticmethod
    def _restore_member(source: Path, target: Path) -> None:
        missing = [parent for parent in target.parents if not parent.exists()]
        for parent in reversed(missing):
            parent.mkdir(mode=0o700)
            Platform.current().sync_directory(parent.parent)
        descriptor, temporary = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
        os.close(descriptor)
        stage = Path(temporary)
        try:
            shutil.copy2(source, stage)
            AtomicFile(target).replace(stage)
        finally:
            stage.unlink(missing_ok=True)

    @classmethod
    def restore_file(cls, path: Path, name: Path, directory: Path, saved: Mapping[Path, str]) -> None:
        for target in cls.files(path):
            retained = cls.relocated(path, name, target)
            if retained in saved:
                cls._restore_member(directory / retained, target)
            else:
                target.unlink(missing_ok=True)
        Platform.current().sync_directory(path.parent)

    @staticmethod
    @contextmanager
    def writers_drained(path: Path) -> Iterator[None]:
        """A file no writer locks has no write in flight to wait out."""
        yield

    @staticmethod
    def settle(path: Path) -> None:
        """A plain file keeps no committed bytes beside it, so there is nothing to settle."""

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
        for family in cls.families():
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
    def retained_name(cls, root: Path, path: Path) -> Path:
        return path.relative_to(root)

    @classmethod
    def source_path(cls, root: Path, original_root: Path, name: Path) -> Path:
        return root / name

    @classmethod
    def external_members(cls, root: Path, original_root: Path) -> Mapping[Path, type[RootFile]]:
        return {}

    @classmethod
    def require_retained_name(cls, name: Path) -> None:
        """Declarations with a reserved namespace validate it here."""

    @classmethod
    def installation_declarations(cls, root: Path, original_root: Path) -> dict[Path, type[RootFile]]:
        declarations = {
            member: owner.declaring_owner()
            for owner, directory in cls.declarations_in(root)
            for member in owner.files_in(directory)
        }
        declarations.update((path, owner.declaring_owner())
                            for path, owner in cls.primaries_in(root).items())
        declarations.update(cls.external_declarations(root, original_root))
        return declarations

    @classmethod
    def families(cls) -> Iterator[type[Self]]:
        pending = [cls]
        visited = set()
        while pending:
            family = pending.pop()
            if family in visited:
                continue
            visited.add(family)
            yield family
            pending.extend(family.__subclasses__())

    @classmethod
    def external_declarations(cls, root: Path, original_root: Path) -> dict[Path, type[RootFile]]:
        return {path: owner for family in cls.families()
                for path, owner in family.external_members(root, original_root).items()}

    @classmethod
    def referenced_files(cls, root: Path, original_root: Path, path: Path
                         ) -> dict[DeclaredFile | UndeclaredFile, type[RootFile]]:
        if path.is_absolute():
            return cls.owned_files(root, (path,), original_root)
        return cls.named_files(root, (path,), original_root)

    @classmethod
    def named_files(cls, root: Path, names: Collection[Path], original_root: Path
                    ) -> dict[DeclaredFile | UndeclaredFile, type[RootFile]]:
        declarations = cls.installation_declarations(root, original_root)
        known = {owner.retained_name(root, path): path for path, owner in declarations.items()}
        for name in names:
            if name.is_absolute() or ".." in name.parts:
                raise RelationViolationError(f"Installation refuses a retained name: {name}")
            for family in cls.families():
                family.require_retained_name(name)
        return cls.owned_files(root, tuple(known.get(name, root / name) for name in names),
                               original_root)

    @classmethod
    def owned_files(
        cls, root: Path, paths: Collection[Path], original_root: Path,
    ) -> dict[DeclaredFile | UndeclaredFile, type[RootFile]]:
        """Resolve primaries once; companions belong to their primary's declaration.

        Missing selected files still identify an absent resource to restore. An
        undeclared existing file acquires content behavior without inventing a
        store declaration for it.
        """
        declarations = cls.installation_declarations(root, original_root)
        primaries = cls.primaries_in(root)
        primaries.update((path, owner) for path, owner in cls.external_declarations(
            root, original_root).items() if path.is_file())
        selected = frozenset(paths)
        owners: dict[Path, type[RootFile]] = {
            path: owner for path, owner in primaries.items() if path in selected
        }
        companions = {member for path, owner in owners.items()
                      for member in (*owner.files(path), *owner.transient_paths(path))
                      if member != path}
        owners.update((path, declarations.get(path, RootFile))
                      for path in selected - owners.keys() - companions)
        companions.update(member for path, owner in owners.items()
                          for member in (*owner.files(path), *owner.transient_paths(path))
                          if member != path)
        # A lock is never content, even after a carry deletes the store it locked.
        companions.update(path for path in selected if is_store_lock(path))
        resources = {}
        names: set[Path] = set()
        for path, owner in owners.items():
            if path in companions:
                continue
            if path.resolve() != path:
                raise RelationViolationError(f"Installation refuses an aliased file: {path}")
            if path not in declarations and not path.is_relative_to(root):
                raise RelationViolationError(f"Installation refuses an undeclared file: {path}")
            name = owner.retained_name(root, path)
            resource = (DeclaredFile(declarations[path], name) if path in declarations
                        else UndeclaredFile(name))
            if name in names:
                raise RelationViolationError(f"Installation file names collide: {name}")
            names.add(name)
            resources[resource] = owner
        return resources

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


@dataclass(frozen=True)
class UndeclaredFile(RootFile):
    """A physical file without a stored declaration, still identified by its path."""

    owner: ClassVar[type[RootFile]] = RootFile
    path: Path
    declared: ClassVar[bool] = False
    """No declaration answers whether its writers take a lock, so a lock beside it is unknown."""


class LockedFile(RootFile, StoreLockOwner, declared_name=None):
    """A declared file whose writers hold its store lock, so its copy shares that lock."""

    __slots__ = ()

    @classmethod
    def copy_group(cls, source: Path, target: Path) -> frozenset[Path]:
        with _store_lock(source, mode=SharedLock()):
            return super().copy_group(source, target)

    @classmethod
    @contextmanager
    def writers_drained(cls, path: Path) -> Iterator[None]:
        """Its writers ask maintenance under this lock, so holding it waits out the last."""
        with _store_lock(path):
            yield


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class FileStore(RootFile):
    """Own file-path construction, state and the factory for file-constructed members."""

    path: Path

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


class WrittenFile(RootFile, ABC):
    """A file whose kind declares when maintenance permits its writers."""

    __slots__ = ()
    if TYPE_CHECKING:
        @property
        def path(self) -> Path: ...

    @property
    def store(self) -> Self:
        """A file writer publishes to itself; borrowed writers name their file instead."""
        return self

    @abstractmethod
    def require_maintenance_open(self) -> None:
        """Refuse writes that this file kind does not permit during maintenance."""


class RuntimeFile(WrittenFile):
    """A bus file no carry writes: running owners rewrite it at any time, maintenance included."""

    __slots__ = ()

    def require_maintenance_open(self) -> None:
        """No installation reads this file, so maintenance never refuses its writers."""


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
        """The copy names no live process: a signal, stop or wake aimed through it would reach
        the source root's units."""
        from .registry_store import RegistryStore

        registry = RegistryStore(source)
        with registry.locked(mode=SharedLock()):
            copied = frozenset(path for path in registry.cut_paths if path.exists())
            for path in copied:
                destination = target.parent / path.relative_to(source.parent)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cls.copy_file(path, destination)
        RegistryStore(target).retire_processes()
        return copied




class SQLiteStorage(RootFile, SqliteStore):
    """SQLite connections, transactions and companion membership shared by file policies."""

    @staticmethod
    def relocated(source: Path, target: Path, member: Path) -> Path:
        return target.with_name(target.name + member.name.removeprefix(source.name))

    __slots__ = ()
    wal_suffix: ClassVar[str] = "-wal"
    transient_suffixes: ClassVar[tuple[str, ...]] = ("-shm", "-journal")

    @classmethod
    def writer(
        cls, path: str | os.PathLike[str], contention: StoreContention = DEFAULT_STORE_CONTENTION,
        *, isolation_level: _IsolationLevel = DEFAULT_ISOLATION,
        uri: bool = False, check_same_thread: bool = True,
    ) -> sqlite3.Connection:
        """Open a writer through the store family's typed busy behavior."""
        return cls.identifiers_only(contention.writer(
            path, isolation_level=isolation_level, uri=uri, check_same_thread=check_same_thread,
        ))

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


class SQLiteFile(SQLiteStorage):
    """A bus SQLite file retains logical snapshots after its writers enter maintenance."""

    @classmethod
    def copy_file(cls, source: str | Path, target: str | Path) -> Path:
        source, target = Path(source), Path(target)
        with (closing(cls.read_snapshot(source)) as original,
              closing(cls.writer(target)) as copied):
            original.backup(copied)
        shutil.copystat(source, target)
        return target

    @classmethod
    @contextmanager
    def writers_drained(cls, path: Path) -> Iterator[None]:
        with closing(cls.writer(path, isolation_level=None)) as held:
            held.execute("BEGIN IMMEDIATE")
            try:
                yield
            finally:
                held.execute("ROLLBACK")

    @classmethod
    def settle(cls, path: Path) -> None:
        wal = cls.wal_path(path)
        if not (wal.is_file() and wal.stat().st_size):
            return
        with cls.exclusive_writer(path) as db:
            busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise sqlite3.OperationalError(f"Another connection holds the WAL of {path}")

    @classmethod
    def digest(cls, path: Path) -> str:
        with closing(cls.read_snapshot(path)) as database:
            content = hashlib.sha256()
            for statement in database.iterdump():
                content.update(statement.encode())
            return content.hexdigest()


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
    def transient_paths(cls, path: Path) -> frozenset[Path]:
        return super().transient_paths(path) | frozenset(cls._source_paths(path))

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
