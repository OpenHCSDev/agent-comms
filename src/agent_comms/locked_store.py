"""Typed JSON documents with canonical locks and durable atomic replacement."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from inspect import isabstract
from typing import Any, ClassVar, Generic, TypeVar

from .field_codec import FieldCodec, MalformedRecord
from .store_files import ExclusiveLock, LockMode, SharedLock, StoreLock, _store_lock

T = TypeVar("T")


class JsonDocument(ABC, Generic[T]):
    """Declared JSON codec and atomic file I/O; LockedStore owns transactions."""

    path: Path
    json_indent: ClassVar[int | None] = None
    json_sort_keys: ClassVar[bool] = False
    json_suffix: ClassVar[str] = ""

    @property
    @abstractmethod
    def record_type(self) -> type[T]:
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
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        staged = Path(temporary)
        backup = staged.with_suffix(".previous")
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
    def at(cls, root: Path | str, *args, **kwargs):
        return cls(cls.path_in(root), *args, **kwargs)


@dataclass(frozen=True, slots=True)
class LockedStore(JsonDocument[T]):
    """Ordered document transactions; callbacks never mutate or reenter."""

    path: Path
    lock_rank: ClassVar[int]

    def __init_subclass__(cls, **kwargs):
        super(LockedStore, cls).__init_subclass__(**kwargs)
        rank = vars(cls).get("lock_rank")
        if not isabstract(cls) and rank is None:
            raise TypeError(f"{cls.__name__} must declare its own lock_rank")

    @contextmanager
    def locked(self, *, mode: LockMode = ExclusiveLock(), blocking: bool = True) -> Iterator[int]:
        """Canonical lock, including a descriptor for scoped child authority."""
        with _store_lock(self.path, mode=mode, blocking=blocking) as lock:
            yield lock.descriptor

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
