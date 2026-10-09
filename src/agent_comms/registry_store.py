"""A8 registry persistence: cached documents and the existing private durability guard."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .errors import RelationViolationError
from .field_codec import FieldCodec
from .locked_store import LockedStore
from .private_registry_guard import PrivateRegistryGuard
from .registry_document import RegistryDocument
from .store_files import file_revision


@dataclass(frozen=True, slots=True)
class RegistryRevision:
    revision: tuple[int, int, int, int]
    document: RegistryDocument


@dataclass(slots=True)
class RegistryCache:
    """The decoded registry belongs to its file, not to one store object.

    Every store for one path in this process shares this cache, so a new
    Comms or Registration reads a known revision without decoding it again.
    The revision and its document are swapped as one value because reader
    threads share the cache.
    """

    entry: RegistryRevision | None = None
    # Observers wake on the same registry change from several threads; one
    # decodes the new revision and the others take its result.
    decoding: threading.Lock = field(default_factory=threading.Lock)
    # A successful guard check, keyed by the revisions of the three files it
    # reads. Unchanged files give the same answer; failures are never kept.
    guard: tuple[tuple, PrivateRegistryGuard | None] | None = None

    _by_path: ClassVar[dict[str, RegistryCache]] = {}

    @classmethod
    def for_path(cls, path: Path) -> RegistryCache:
        return cls._by_path.setdefault(os.path.abspath(path), cls())


@dataclass(slots=True)
class RegistryEdit:
    store: RegistryStore
    document: RegistryDocument
    original: RegistryDocument

    def commit(self) -> None:
        if self.document != self.original:
            self.store.save_unlocked(self.document)
            self.original = self.document.copy()


@dataclass(frozen=True, slots=True)
class RegistryStore(LockedStore[RegistryDocument]):
    json_indent = 2
    cache: RegistryCache = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cache", RegistryCache.for_path(self.path))

    @property
    def record_type(self) -> type[RegistryDocument]:
        return RegistryDocument

    def empty(self) -> RegistryDocument:
        return RegistryDocument()

    def _read_unlocked(self) -> RegistryDocument:
        self.private_guard_unlocked()  # before even a cache hit
        revision = file_revision(self.path)
        if revision is None:
            return self.empty()
        entry = self.cache.entry
        if entry is not None and entry.revision == revision:
            return entry.document
        with self.cache.decoding:
            entry = self.cache.entry
            if entry is not None and entry.revision == revision:
                return entry.document
            document = self._decode(json.loads(self.path.read_text()))
            self.cache.entry = RegistryRevision(revision, document)
            return document

    def _encode(self, value: RegistryDocument) -> dict:
        return FieldCodec.encode(value)

    def _decode(self, data: object) -> RegistryDocument:
        return self.record_type.from_wire(data)

    @contextmanager
    def editing(self) -> Iterator[RegistryEdit]:
        with self.locked():
            original = self._read_unlocked()
            yield RegistryEdit(self, original.copy(), original)

    def save_unlocked(self, document: RegistryDocument) -> None:
        self._write_unlocked(json.dumps(self._encode(document), indent=2))
        # Still under the exclusive lock: the written file is this document.
        if (revision := file_revision(self.path)) is not None:
            self.cache.entry = RegistryRevision(revision, document.copy())

    def _write_unlocked(self, text: str) -> None:
        # The two-file private guard protocol cannot use A8's rollback writer:
        # a pending guard after any failed write MUST remain fail-closed.
        from . import store_files

        self.cache.entry = None
        guard = self.private_guard_unlocked()
        if guard is None:
            store_files._atomic_write_text(self.path, text, fsync_parent=True)
        else:
            digest = hashlib.sha256(b"present\0" + text.encode("utf-8")).digest()
            sequence, slot = guard.prepare(digest)
            store_files._atomic_write_text(self.path, text, fsync_parent=True)
            guard.commit(sequence, slot, digest)

    def private_guard_unlocked(self) -> PrivateRegistryGuard | None:
        """Validate the private marker/guard BEFORE even a cached registry read.

        The directory, guard file and registry are one private root. A marker
        without its committed guard (or a guard without a marker) is an
        uncertain migration, never an invitation to bootstrap old metadata.
        """
        from .private_registry_guard import PrivateRegistryGuard

        marker_path = self.path.parent / "bus_meta.json"
        guard_path = self.path.parent / ".registry-owner-guard"
        key = (_link_revision(self.path.parent), _link_revision(marker_path), file_revision(marker_path),
               _link_revision(guard_path), _link_revision(self.path))
        if (checked := self.cache.guard) is not None and checked[0] == key:
            return checked[1]
        guard = self._check_guard_unlocked(marker_path, guard_path)
        self.cache.guard = key, guard
        return guard

    def _check_guard_unlocked(self, marker_path: Path, guard_path: Path) -> PrivateRegistryGuard | None:
        from .private_registry_guard import PrivateRegistryGuard

        guard_present = guard_path.exists() or guard_path.is_symlink()
        if not marker_path.exists() and not marker_path.is_symlink():
            if guard_present:
                raise RelationViolationError("Private registry guard has no protocol marker")
            return None
        from .wire_log import WireLog

        marker = WireLog(self.path.with_name("bus.jsonl")).read_metadata_unlocked(required=True)
        if not marker.private:
            if guard_present:
                raise RelationViolationError("Private registry guard marker is absent")
            return None
        guard = PrivateRegistryGuard(self.path, marker.root_id)
        guard.verify()
        return guard


def _link_revision(path: Path) -> tuple[int, int, int, int] | None:
    """Revision of the directory entry itself, without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
