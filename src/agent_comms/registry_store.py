"""A8 registry persistence: cached documents and the existing private durability guard."""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from .coordination_database import CoordinationStore
from .errors import RelationViolationError
from .field_codec import FieldCodec, NullMember
from .locked_store import RegistryFile
from .maintenance_barrier import CarriedDocument
from .native_input_owner import RegistryOwner
from .private_path import FileObservation, FileRevision, MissingFile
from .private_registry_guard import PrivateRegistryGuard, PublicRegistry, RegistryPrivacy
from .registration_change import NativeSourcePublication
from .registry_document import (
    HistoricalRootPurpose,
    RegistryDocument,
    RegistryEntry,
    RegistrySnapshot,
)
from .routing import TurnRouting
from .store_files import SharedLock
from .thread_identity import TurnIdentity
from .session_file import SavedSessionFile
from .threads import Thread
from .turn_lease import ActiveTurn, RecordedGeneration, StoredTurnRecord


class CachedRegistry(ABC):
    """The registry revision this process last decoded, or none yet."""

    @abstractmethod
    def revision_at(
        self, revision: FileObservation, decode: Callable[[CachedRegistry], RegistryRevision],
    ) -> RegistryRevision:
        """This revision when it is current, else ``decode`` from it."""

    @abstractmethod
    def entry_at(
        self, revision: FileRevision, name: str, decode: Callable[[], RegistryEntry],
    ) -> RegistryEntry:
        """One thread's entry from this revision when it is current, else ``decode``."""

    @abstractmethod
    def decoded(self, store: RegistryStore, raw: dict) -> RegistryDocument:
        """Decode ``raw``, reusing the threads this revision already decoded."""


@dataclass(frozen=True)
class UncachedRegistry(NullMember, CachedRegistry):
    def revision_at(
        self, revision: FileObservation, decode: Callable[[CachedRegistry], RegistryRevision],
    ) -> RegistryRevision:
        return decode(self)

    def entry_at(
        self, revision: FileRevision, name: str, decode: Callable[[], RegistryEntry],
    ) -> RegistryEntry:
        return decode()

    def decoded(self, store: RegistryStore, raw: dict) -> RegistryDocument:
        return store._decode(raw)


@dataclass(frozen=True)
class RegistryRevision(CachedRegistry):
    """A registry file revision: its document and the raw rows it was decoded from.

    The raw rows let the next revision decode only the threads that changed.
    A missing file's rows are the empty document's.
    """

    revision: FileObservation
    document: RegistryDocument
    raw: dict

    def revision_at(
        self, revision: FileObservation, decode: Callable[[CachedRegistry], RegistryRevision],
    ) -> RegistryRevision:
        return self if self.revision == revision else decode(self)

    def entry_at(
        self, revision: FileRevision, name: str, decode: Callable[[], RegistryEntry],
    ) -> RegistryEntry:
        return self.document.entry(name) if self.revision == revision else decode()

    def decoded(self, store: RegistryStore, raw: dict) -> RegistryDocument:
        return store._bind_document(RegistryDocument.from_wire(raw, (self.raw, self.document)))

    @cached_property
    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot.capture(self.document)


@dataclass(frozen=True, slots=True)
class RegistryEntryRevision:
    """One thread's entry and the registry revision it was read from, under one lock."""

    revision: tuple[FileObservation, FileObservation]
    entry: RegistryEntry


@dataclass(slots=True)
class RegistryCache:
    """The decoded registry belongs to its file, not to one store object.

    Every store for one path in this process shares this cache, so a new
    Comms or Registration reads a known revision without decoding it again.
    The revision and its document are swapped as one value because reader
    threads share the cache.
    """

    entry: CachedRegistry = field(default_factory=UncachedRegistry)
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
    original: RegistryRevision

    def prepare_native_source(self, owner: RegistryOwner, session_file: str) -> NativeSourcePublication:
        current = owner.require_source_snapshot(self.original.snapshot)
        status = self.document.status(current.name)
        return NativeSourcePublication(
            thread=replace(current, session_file=SavedSessionFile(session_file)), status=status,
            previous=current, previous_status=status,
        )

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
            current.require_process(),
            routing=routing,
            claimed_generation=RecordedGeneration(self.document.claims.generations[current.name]),
            turn_generation=RecordedGeneration(identity.generation),
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
        if self.document != self.original.document:
            self.store.save_unlocked(self.document)
            self.original = self.store.read_revision_unlocked()


@dataclass(frozen=True, slots=True)
class RegistryStore(RegistryFile, CarriedDocument[RegistryDocument]):
    json_indent = 2
    cache: RegistryCache = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        from .private_runtime_schema import PrivateCoordinatorTable

        # Guard-loss checks must know every private declaration before taking this store's lock.
        PrivateCoordinatorTable.declarations()
        object.__setattr__(self, "cache", RegistryCache.for_path(self.path))

    record_type = RegistryDocument

    def empty(self) -> RegistryDocument:
        return RegistryDocument()

    def _read_unlocked(self) -> RegistryDocument:
        return self.read_revision_unlocked().document

    def read_revision_unlocked(self) -> RegistryRevision:
        self.private_guard_unlocked()  # before even a cache hit
        revision = FileRevision.capture(self.path)
        return self.cache.entry.revision_at(revision, lambda stale: self._decode_revision_unlocked(revision))

    def _decode_revision_unlocked(self, revision: FileObservation) -> RegistryRevision:
        with self.cache.decoding:
            return self.cache.entry.revision_at(
                revision, lambda stale: self._cache_revision_unlocked(stale, revision))

    def _cache_revision_unlocked(self, stale: CachedRegistry, revision: FileObservation) -> RegistryRevision:
        raw = revision.either(
            lambda present: FieldCodec.parse(self.path.read_bytes()), lambda: self._encode(self.empty()))
        entry = RegistryRevision(revision, stale.decoded(self, raw), raw)
        self.cache.entry = entry
        return entry

    def read_snapshot_unlocked(self) -> RegistrySnapshot:
        revision = self.read_revision_unlocked()
        # Shared readers may be the first to ask for this revision's snapshot.
        # Use the existing decode mutex for that one construction, not the
        # whole caller's read lifetime.
        with self.cache.decoding:
            return revision.snapshot

    @contextmanager
    def reading_snapshot(self) -> Iterator[RegistrySnapshot]:
        with self.locked(mode=SharedLock()):
            yield self.read_snapshot_unlocked()

    @property
    def guard_path(self) -> Path:
        return PrivateRegistryGuard.path_in(self.path.parent)

    @property
    def written(self) -> tuple[Path, Path]:
        """The files every write replaces together, as ``revision_unlocked`` reads them."""
        return self.path, self.guard_path

    @property
    def cut_paths(self) -> tuple[Path, ...]:
        """The files one hold of the registry lock keeps consistent: every write replaces
        ``written`` together, and a turn claim publishes its turn file in the same
        transaction. Read under that lock, so a claim cannot fall between them."""
        return (*self.written, *sorted(StoredTurnRecord.matching_in(self.path.parent)))

    def revision_unlocked(self) -> tuple[FileObservation, FileObservation]:
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
            return RegistryEntryRevision(revision, revision[0].either(
                lambda present: self.cache.entry.entry_at(
                    present, name, lambda: self._decode_entry_unlocked(name)),
                lambda: self.empty().entry(name),
            ))

    def _decode_entry_unlocked(self, name: str) -> RegistryEntry:
        decoded_entry = RegistryDocument.entry_from_wire(FieldCodec.parse(self.path.read_bytes()), name)
        return replace(decoded_entry, thread=self._bind_thread(decoded_entry.thread))

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
            original = self.read_revision_unlocked()
            original.document.purpose.require_write()
            yield RegistryEdit(self, original.document.copy(), original)

    def mark_historical(self) -> None:
        """Explicit archive creation/carry seals this declaration, idempotently."""
        with self.locked():
            original = self.read_revision_unlocked()
            document = replace(original.document, purpose=HistoricalRootPurpose())
            RegistryEdit(self, document, original).commit()

    def save_unlocked(self, document: RegistryDocument) -> None:
        self.read_revision_unlocked().document.purpose.require_write()
        raw = self._encode(document)
        self._write_unlocked(json.dumps(raw, indent=2))
        # Still under the exclusive lock: the written file is this document.
        for revision in FileRevision.capture(self.path).revisions():
            self.cache.entry = RegistryRevision(revision, document.copy(), raw)

    def _replace_unlocked(self, text: str) -> None:
        # The two-file private guard protocol cannot use A8's rollback writer.
        self.cache.entry = UncachedRegistry()
        self.private_guard_unlocked().write(self.path, text)

    def private_guard_unlocked(self) -> RegistryPrivacy:
        """Validate registry authority before even a cached document read.

        The guard owns its native namespace. Native runtime tables without
        that guard are incomplete state, never a new namespace to initialize.
        """
        key = (
            _link_revision(self.path.parent),
            _link_revision(CoordinationStore.path_in(self.path.parent)),
            _link_revision(self.guard_path),
            _link_revision(self.path),
        )
        if (checked := self.cache.guard) is not None and checked[0] == key:
            return checked[1]
        guard = self._check_guard_unlocked()
        self.cache.guard = key, guard
        return guard

    def _check_guard_unlocked(self) -> RegistryPrivacy:
        if self.guard_path.exists() or self.guard_path.is_symlink():
            return PrivateRegistryGuard.load(self.path)
        coordinator = CoordinationStore.path_in(self.path.parent)
        if coordinator.exists() or coordinator.is_symlink():
            from .private_runtime_schema import PrivateCoordinatorTable

            with CoordinationStore.observing(coordinator) as db:
                if PrivateCoordinatorTable.stored_schema(db):
                    raise RelationViolationError("Native runtime has no registry authority")
        return PublicRegistry()


def _link_revision(path: Path) -> FileObservation:
    """Revision of the directory entry itself, without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return MissingFile()
    return FileRevision.from_stat(info)
