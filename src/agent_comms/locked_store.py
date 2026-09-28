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
from typing import Any, ClassVar, Generic, TypeVar

from .declarations import _replace_snapshot, _store_lock
from .field_codec import FieldCodec

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LockedStore(ABC, Generic[T]):
    """One document owner; subclasses declare its type and missing-file value.

    Update callbacks return a replacement value, or the original object to skip
    writing. They must not mutate the original or call this store recursively.
    No public method may be called while holding this document's canonical lock
    on another descriptor. Distinct outer locks (for example wire) are allowed.
    The append-only wire is not a document and must not use this abstraction.
    """

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
        try:
            text = self.path.read_text(encoding="utf-8")
            return self._decode(json.loads(text))
        except FileNotFoundError:
            return self.empty()
        except (OSError, ValueError, TypeError) as error:
            return self._unreadable(error)

    def _unreadable(self, error: Exception) -> T:
        """Strict by default; optional owners may declare a fail-closed value."""
        raise error

    def _decode(self, data: Any) -> T:
        """Decode at the boundary; owners may normalize legacy document shapes."""
        return FieldCodec.decode(self.record_type, data)

    def _encode(self, value: T) -> Any:
        """Owners with an established external shape may project that encoding."""
        return FieldCodec.encode(value)

    @contextmanager
    def locked(self, *, shared: bool = False, blocking: bool = True) -> Iterator[int]:
        """Canonical lock, including a descriptor for scoped child authority."""
        with _store_lock(self.path, shared=shared, blocking=blocking) as descriptor:
            yield descriptor

    @contextmanager
    def reading(self) -> Iterator[T]:
        """Keep a shared lock through a dependent projection or source check."""
        with self.locked(shared=True):
            yield self._read_unlocked()

    def read(self) -> T:
        with self.reading() as value:
            return value

    def update(self, change: Callable[[T], T]) -> T:
        with self.locked():
            original = self._read_unlocked()
            changed = change(original)
            if changed is not original:
                self._write_unlocked(
                    json.dumps(
                        self._encode(changed),
                        indent=self.json_indent,
                        sort_keys=self.json_sort_keys,
                    )
                    + self.json_suffix
                )
            return changed

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
                    self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
            try:
                os.link(self.path, backup)
                had_original = True
            except FileNotFoundError:
                pass
            _replace_snapshot(staged, self.path)
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
                        _replace_snapshot(backup, self.path)
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
