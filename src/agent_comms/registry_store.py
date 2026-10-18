"""A8 registry persistence: cached documents and the existing private durability guard."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from .errors import RelationViolationError
from .field_codec import FieldCodec
from .locked_store import LockedStore
from .private_path import FileRevision
from .private_registry_guard import PrivateRegistryGuard, PublicRegistry, RegistryPrivacy
from .registry_document import RegistryDocument, RegistryEntry
from .routing import TurnRouting
from .store_files import SharedLock
from .thread_identity import TurnIdentity
from .threads import Thread
from .turn_lease import ActiveTurn, StoredTurnRecord


@dataclass(frozen=True, slots=True)
class RegistryRevision:
    """A registry file revision: its document and the raw rows it was decoded from.

    The raw rows let the next revision decode only the threads that changed.
    """

    revision: FileRevision | None
    document: RegistryDocument
    raw: dict


@dataclass(frozen=True, slots=True)
class RegistryEntryRevision:
    """One thread's entry and the registry revision it was read from, under one lock."""

    revision: tuple[FileRevision | None, FileRevision | None]
    entry: RegistryEntry


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
    guard: tuple[tuple, RegistryPrivacy] | None = None

    _by_path: ClassVar[dict[str, RegistryCache]] = {}

    @classmethod
    def for_path(cls, path: Path) -> RegistryCache:
        return cls._by_path.setdefault(os.path.abspath(path), cls())


@dataclass(slots=True)
class RegistryEdit:
    store: RegistryStore
    document: RegistryDocument
    original: RegistryDocument

    def lease_turn(
        self,
        current: Thread,
        turn_id: str,
        routing: TurnRouting | None,
    ) -> tuple[Thread, int]:
        """Allocate the original record under this bound registry transaction."""
        if current.turn_generation >= (1 << 63) - 1:
            raise RelationViolationError("Turn generation exhausted")
        identity = TurnIdentity(current.incarnation, current.turn_generation + 1)
        turn = ActiveTurn(
            turn_id,
            current.require_process().pid,
            routing=routing,
            claimed_generation=self.document.claims.generations[current.name],
            turn_generation=identity.generation,
        )
        leased = replace(
            current,
            turn_generation=identity.generation,
            last_finished_turn_id=None,
            project_resume_due=False,
            turn_record=StoredTurnRecord.begin(self.store.path, identity, turn),
        )
        self.document.threads[current.name] = leased
        self.document.last_seen[current.name] = time.time()
        return leased, self.document.owners.generations[current.name]

    def commit(self) -> None:
        if self.document != self.original:
            self.store.save_unlocked(self.document)
            self.original = self.document.copy()


@dataclass(frozen=True, slots=True)
class RegistryStore(LockedStore[RegistryDocument]):
    lock_rank = 2
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
        revision = FileRevision.capture(self.path)
        if revision is None:
            return self.empty()
        entry = self.cache.entry
        if entry is not None and entry.revision == revision:
            return entry.document
        with self.cache.decoding:
            entry = self.cache.entry
            if entry is not None and entry.revision == revision:
                return entry.document
            raw = json.loads(self.path.read_text())
            if entry is None:
                document = self._decode(raw)
            else:
                document = self._bind_document(
                    RegistryDocument.from_wire(raw, (entry.raw, entry.document))
                )
            self.cache.entry = RegistryRevision(revision, document, raw)
            return document

    @property
    def guard_path(self) -> Path:
        return self.path.parent / ".registry-owner-guard"

    def revision_unlocked(self) -> tuple[FileRevision | None, FileRevision | None]:
        """The registry file and its owner guard, which every write replaces together."""
        return FileRevision.capture(self.path), FileRevision.capture(self.guard_path)

    def read_entry(self, name: str) -> RegistryEntryRevision:
        """One thread's entry from the current file, without decoding the other threads.

        The revision is taken under the same lock, so it is the one the entry
        was read from, not an earlier stat taken while a write was in flight.
        A decoded revision already in this process's cache answers directly.
        """
        with self.locked(mode=SharedLock()):
            self.private_guard_unlocked()
            revision = self.revision_unlocked()
            if revision[0] is None:
                return RegistryEntryRevision(revision, self.empty().entry(name))
            entry = self.cache.entry
            if entry is not None and entry.revision == revision[0]:
                return RegistryEntryRevision(revision, entry.document.entry(name))
            raw = json.loads(self.path.read_text())
            decoded_entry = RegistryDocument.entry_from_wire(raw, name)
            return RegistryEntryRevision(
                revision, replace(decoded_entry, thread=self._bind_thread(decoded_entry.thread))
            )

    def _encode(self, value: RegistryDocument) -> dict:
        return FieldCodec.encode(value)

    def _bind_thread(self, thread: Thread) -> Thread:
        record = thread.turn_record
        bound = record.bound_to(self.path, TurnIdentity(thread.incarnation, thread.turn_generation))
        return thread if bound is record else replace(thread, turn_record=bound)

    def _bind_document(self, document: RegistryDocument) -> RegistryDocument:
        for name, thread in document.threads.items():
            document.threads[name] = self._bind_thread(thread)
        return document

    def _decode(self, data: object) -> RegistryDocument:
        return self._bind_document(self.record_type.from_wire(data))

    @contextmanager
    def editing(self) -> Iterator[RegistryEdit]:
        with self.locked():
            original = self._read_unlocked()
            yield RegistryEdit(self, original.copy(), original)

    def save_unlocked(self, document: RegistryDocument) -> None:
        raw = self._encode(document)
        self._write_unlocked(json.dumps(raw, indent=2))
        # Still under the exclusive lock: the written file is this document.
        if (revision := FileRevision.capture(self.path)) is not None:
            self.cache.entry = RegistryRevision(revision, document.copy(), raw)

    def _write_unlocked(self, text: str) -> None:
        # The two-file private guard protocol cannot use A8's rollback writer.
        self.cache.entry = None
        self.private_guard_unlocked().write(self.path, text)

    def private_guard_unlocked(self) -> RegistryPrivacy:
        """Validate the private marker/guard BEFORE even a cached registry read.

        The directory, guard file and registry are one private root. A marker
        without its committed guard (or a guard without a marker) is an
        uncertain migration, never an invitation to bootstrap old metadata.
        """
        marker_path = self.path.parent / "bus_meta.json"
        guard_path = self.guard_path
        key = (
            _link_revision(self.path.parent),
            _link_revision(marker_path),
            FileRevision.capture(marker_path),
            _link_revision(guard_path),
            _link_revision(self.path),
        )
        if (checked := self.cache.guard) is not None and checked[0] == key:
            return checked[1]
        guard = self._check_guard_unlocked(marker_path, guard_path)
        self.cache.guard = key, guard
        return guard

    def _check_guard_unlocked(self, marker_path: Path, guard_path: Path) -> RegistryPrivacy:
        guard_present = guard_path.exists() or guard_path.is_symlink()
        if not marker_path.exists() and not marker_path.is_symlink():
            if guard_present:
                raise RelationViolationError("Private registry guard has no protocol marker")
            return PublicRegistry()
        from .wire_log import WireLog

        marker = WireLog(self.path.with_name("bus.jsonl")).read_metadata_unlocked(required=True)
        if not marker.private:
            if guard_present:
                raise RelationViolationError("Private registry guard marker is absent")
            return PublicRegistry()
        guard = PrivateRegistryGuard(self.path, marker.root_id)
        guard.verify()
        return guard


def _link_revision(path: Path) -> FileRevision | None:
    """Revision of the directory entry itself, without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return FileRevision.from_stat(info)
