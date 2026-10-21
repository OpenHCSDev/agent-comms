"""Typed JSON documents with canonical locks and durable atomic replacement."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from pkgutil import walk_packages
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar

from .field_codec import FieldCodec, MalformedRecord
from .store_files import SharedLock, StoreLock, StoreLockOwner, StoreOwner, store_lock_path

if TYPE_CHECKING:
    import sqlite3

T = TypeVar("T")


class JsonDocument(ABC, Generic[T]):
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

    def _read_using(self, decode: Callable[[object], T]) -> T:
        try:
            return decode(FieldCodec.parse(self.path.read_bytes()))
        except FileNotFoundError:
            return self.empty()
        except (OSError, MalformedRecord) as error:
            return self._unreadable(error)

    def _unreadable(self, error: Exception) -> T:
        """Strict by default; optional owners may declare a fail-closed value."""
        raise error

    def _decode(self, data: Any) -> T:
        """Decode the current declaration at the storage boundary."""
        return FieldCodec.decode_document(self.record_type, data)

    def _encode(self, value: T) -> Any:
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
        backup = StoreOwner.rollback_file(staged)
        directory_fd = None
        published = False
        had_original = False
        keep_backup = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                try:
                    mode = stat.S_IMODE(self.path.stat().st_mode)
                except FileNotFoundError:
                    mode = 0o600
                output.write(text)
                output.flush()
                os.chmod(staged, mode)
                os.fsync(output.fileno())
            if os.name == "posix":
                directory_fd = os.open(
                    self.path.parent, os.O_RDONLY | os.O_DIRECTORY
                )
            try:
                os.link(self.path, backup)
                had_original = True
            except FileNotFoundError:
                pass
            os.replace(staged, self.path)
            published = True
            if directory_fd is not None:
                os.fsync(directory_fd)
            # A failed backup cleanup is still a failed update: retain the old
            # inode until unlink succeeds so the exception path can restore it.
            backup.unlink(missing_ok=True)
        except BaseException:
            if published:
                if had_original:
                    try:
                        os.replace(backup, self.path)
                    except BaseException:
                        # Keep the recovery bytes if the filesystem also
                        # refuses rollback; never erase the remaining copy.
                        keep_backup = True
                        raise
                else:
                    self.path.unlink()
                if directory_fd is not None:
                    os.fsync(directory_fd)
            raise
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            staged.unlink(missing_ok=True)
            if not keep_backup:
                backup.unlink(missing_ok=True)


class RootFile:
    """A declared bus-file location, independent of its value or I/O constructor."""

    __slots__ = ()
    filename: ClassVar[str]
    root_directory: ClassVar[str] = "."

    @classmethod
    def path_in(cls, root: Path | str, **parts: str) -> Path:
        return Path(root) / cls.filename.format(**parts)

    @classmethod
    def member_paths(cls, path: Path) -> frozenset[Path]:
        """The resources composed onto one declared primary file."""
        return frozenset((path, store_lock_path(path)))

    @classmethod
    def files_in(cls, root: Path) -> frozenset[Path]:
        return cls.member_paths(cls.path_in(root))

    @classmethod
    def matching_in(cls, root: Path) -> frozenset[Path]:
        """The declared files themselves that exist, without their locks."""
        path = cls.path_in(root)
        return frozenset((path,)) if path.is_file() else frozenset()

    @classmethod
    def load_declarations(cls) -> None:
        """Import every module of the package, so ``declarations_in`` walks the whole family
        and not only the owners some caller happened to import."""
        package = import_module(__package__)
        for module in walk_packages(package.__path__, f"{package.__name__}."):
            if not module.name.startswith(f"{package.__name__}.skills"):
                import_module(module.name)

    @classmethod
    def declarations_in(cls, root: Path) -> Iterator[tuple[type[RootFile], Path]]:
        """Each declaring owner and its directory, once across composed capabilities."""
        pending: list[type[RootFile]] = [cls]
        visited: set[type[RootFile]] = set()
        while pending:
            family = pending.pop()
            if family in visited:
                continue
            visited.add(family)
            pending.extend(family.__subclasses__())
            if "filename" in vars(family):
                yield family, root / family.root_directory

    @classmethod
    def paths_in(cls, root: Path) -> frozenset[Path]:
        return frozenset(
            path for owner, directory in cls.declarations_in(root)
            for path in owner.files_in(directory)
        )


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


class SQLiteFile(RootFile):
    """A database and SQLite's companion files share their existing file owner."""

    __slots__ = ()
    companion_suffixes: ClassVar[tuple[str, ...]] = ("-wal", "-shm", "-journal")

    @classmethod
    def files(cls, path: Path) -> tuple[Path, ...]:
        return (path, *(path.with_name(path.name + suffix) for suffix in cls.companion_suffixes))

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


class RuntimeFile(RootFile):
    """A bus file no carry writes: running owners rewrite it at any time, maintenance included."""

    __slots__ = ()


class DocumentConversion(ABC):
    """A store's declared converter names the old schemas it can actually read."""

    source_fingerprints: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    @abstractmethod
    def converted(cls, raw: dict) -> dict:
        """Convert a supported old document before the current codec validates it."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LockedStore(FileStore, StoreLockOwner, JsonDocument[T]):
    """Ordered document transactions; callbacks never mutate or reenter."""

    path: Path
    conversion: ClassVar[type[DocumentConversion]] = DocumentConversion

    @staticmethod
    def _canonical_schema(value):
        """Schema sets have no order; tuple positions and default arrays do."""
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
                    child = {name: LockedStore._canonical_schema(schema) for name, schema in child.items()}
                elif key not in ("default", "examples", "const", "enum"):
                    child = LockedStore._canonical_schema(child)
                if key in ("required", "enum", "anyOf", "oneOf", "allOf") and isinstance(child, list):
                    child = sorted(child, key=lambda item: json.dumps(item, sort_keys=True))
                result[key] = child
            return result
        if isinstance(value, list):
            return [LockedStore._canonical_schema(child) for child in value]
        return value

    @classmethod
    def format_fingerprint(cls) -> str:
        """The stored declaration, never a manually maintained version number."""
        schema = LockedStore._canonical_schema(FieldCodec.value_schema(cls.record_type))
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @contextmanager
    def reading(self, *sources, blocking: bool = True) -> Iterator[T]:
        """Retain dependent source owners before this document's shared lock."""
        with StoreLock.ordered(dict.fromkeys((*sources, self), SharedLock()), blocking=blocking):
            yield self._read_unlocked()

    def read(self) -> T:
        with self.reading() as value:
            return value

    def update(self, change: Callable[[T], T]) -> T:
        with self.locked():
            original = self._read_unlocked()
            changed = change(original)
            if changed is not original:
                self._publish_unlocked(changed)
            return changed

    def replace(self, value: T) -> None:
        """Publish a complete owned snapshot without reading the discarded one.

        Use update for merges. Derived projections must hold their source lock
        through capture and replacement; this document lock alone is not source
        authority or a cross-document transaction.
        """
        with self.locked():
            self._publish_unlocked(value)
