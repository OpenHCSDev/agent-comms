"""Typed JSON documents with canonical locks and durable atomic replacement."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

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
        except FileNotFoundError:
            return self.empty()
        return self._decode(json.loads(text))

    def _decode(self, data: Any) -> T:
        """Decode at the boundary; owners may normalize legacy document shapes."""
        return FieldCodec.decode(self.record_type, data)

    def read(self) -> T:
        with _store_lock(self.path, shared=True):
            return self._read_unlocked()

    def update(self, change: Callable[[T], T]) -> T:
        with _store_lock(self.path):
            original = self._read_unlocked()
            changed = change(original)
            if changed is not original:
                self._write_unlocked(json.dumps(FieldCodec.encode(changed)))
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
