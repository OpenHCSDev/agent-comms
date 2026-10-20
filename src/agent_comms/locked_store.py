"""Typed JSON documents with canonical locks and durable atomic replacement."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from .field_codec import FieldCodec, MalformedRecord
from .store_files import SharedLock, StoreLock, StoreLockOwner, StoreOwner

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
    """A declared file location, with construction for file-taking owners."""

    __slots__ = ()
    filename: ClassVar[str]

    @classmethod
    def path_in(cls, root: Path | str) -> Path:
        return Path(root) / cls.filename

    @classmethod
    def paths_in(cls, root: Path) -> frozenset[Path]:
        """Every location this family's imported members declare under ``root``."""
        declared, pending = set(), [cls]
        while pending:
            family = pending.pop()
            pending.extend(family.__subclasses__())
            if "filename" in vars(family):
                declared.add(family.path_in(root))
        return frozenset(declared)

    @classmethod
    def at(cls, root: Path | str, *args, **kwargs):
        return cls(cls.path_in(root), *args, **kwargs)


class RuntimeFile(RootFile):
    """A bus file no carry writes: running owners rewrite it at any time, maintenance included."""

    __slots__ = ()


class HistoryDirectory(RootFile):
    """A bus directory of per-thread or per-input history, read only by the thread, input or
    view it names. Starting the bus and one fresh thread's turn never open it."""

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
class LockedStore(StoreLockOwner, JsonDocument[T]):
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
