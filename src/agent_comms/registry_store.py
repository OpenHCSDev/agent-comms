"""A8 registry persistence: cached documents and the existing private durability guard."""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import ClassVar, Self

from .coordination_database import CoordinationStore
from .coordination_errors import StaleFence
from .errors import RelationViolationError
from .field_codec import FieldCodec, NullMember, TurnId
from .locked_store import DocumentBytes, LockedStore, RegistryFile
from .maintenance_barrier import CarriedDocument
from .native_input_owner import RegistryOwner
from .private_path import FileObservation, FileRevision, MissingFile
from .private_registry_guard import PrivateRegistryGuard, PublicRegistry, RegistryPrivacy
from .private_runtime_schema import PrivateCoordinatorTable
from .registration_change import InitialRegistration, NativeSourcePublication
from .registry_document import (
    HistoricalRootPurpose,
    RegistryDocument,
    RegistryEntry,
    RegistrySnapshot,
)
from .routing import Routing
from .session_file import RegistrySessionLocation
from .store_files import RegistryLocking
from .thread_identity import TurnIdentity
from .threads import Thread
from .turn_lease import (
    ActiveTurn, LockedTurn, NamedTurn, RecordedGeneration, RetiredTurn, StoredTurnRecord,
    TurnDeclaration,
)


class CachedRegistry(ABC):
    """The registry revision this process last decoded, or none yet."""

    @abstractmethod
    def at[Answer](
        self, revision: FileObservation, reuse: Callable[[RegistryRevision], Answer],
        read: Callable[[CachedRegistry], Answer],
    ) -> Answer:
        """Reuse this file revision's answer, or read its replacement."""

    @abstractmethod
    def decoded(self, store: RegistryStore, raw: dict) -> RegistryDocument:
        """Decode ``raw``, reusing the threads this revision already decoded."""


@dataclass(frozen=True)
class UncachedRegistry(NullMember, CachedRegistry):
    def at[Answer](
        self, revision: FileObservation, reuse: Callable[[RegistryRevision], Answer],
        read: Callable[[CachedRegistry], Answer],
    ) -> Answer:
        return read(self)

    def decoded(self, store: RegistryStore, raw: dict) -> RegistryDocument:
        return store._decode(raw)


@dataclass(frozen=True)
class RegistryRevision(CachedRegistry):
    """A registry file revision: its document and the raw rows it was decoded from.

    The raw rows let the next revision decode only the threads that changed.
    A missing file's rows are the empty document's.
    """

    source: DocumentBytes
    document: RegistryDocument
    raw: dict

    def at[Answer](
        self, revision: FileObservation, reuse: Callable[[RegistryRevision], Answer],
        read: Callable[[CachedRegistry], Answer],
    ) -> Answer:
        return reuse(self) if self.source.revision == revision else read(self)

    def decoded(self, store: RegistryStore, raw: dict) -> RegistryDocument:
        document = RegistryDocument.from_wire(raw, (self.raw, self.document))
        document.threads.bind(store._bind_thread)
        return document

    @cached_property
    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot.capture(self.document)


@dataclass(frozen=True, slots=True)
class RegistryCut:
    """One decoded registry and its original authority, with store-owned currentness."""

    store: RegistryStore = field(repr=False, compare=False)
    snapshot: RegistrySnapshot
    privacy: RegistryPrivacy
    revision: tuple[FileObservation, FileObservation]

    def matches_current(self) -> bool:
        return self.store.revision_unlocked() == self.revision

    @contextmanager
    def current(self) -> Iterator[None]:
        """Retain the original cut through a bounded dependent write; never refresh it."""
        with self.store.guarded_read():
            if not self.matches_current():
                raise StaleFence("native registry revision changed after this decision read it")
            yield

    @classmethod
    @contextmanager
    def captured(cls, capture: Callable[[], Self]) -> Iterator[Self]:
        """Recapture only before the caller receives its first current decision."""
        cut = capture()
        deadline = time.monotonic() + cut.store.contention.seconds
        while True:
            with cut.store.guarded_read():
                if cut.matches_current():
                    yield cut
                    return
            if time.monotonic() >= deadline:
                raise StaleFence("Registry kept changing before a native decision could begin")
            cut = capture()


@dataclass(frozen=True, slots=True)
class RegistryPairCut[Peer](RegistryCut):
    peer: Peer


@dataclass(frozen=True, slots=True)
class RegistryEntryRevision:
    """One thread's entry and the registry revision it was read from, under one lock."""

    revision: tuple[FileObservation, FileObservation]
    entry: RegistryEntry


@dataclass(slots=True)
class RegistryReadCounts:
    """Cumulative requests, physical captures and decoded revisions in this process."""

    requests: int = 0
    captures: int = 0
    decodes: int = 0


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
    counts: RegistryReadCounts = field(default_factory=RegistryReadCounts)
    measurement: threading.Lock = field(default_factory=threading.Lock)

    def read_counts(self) -> RegistryReadCounts:
        with self.measurement:
            return replace(self.counts)

    _by_path: ClassVar[dict[str, RegistryCache]] = {}

    @classmethod
    def for_path(cls, path: Path) -> RegistryCache:
        return cls._by_path.setdefault(os.path.abspath(path), cls())


@dataclass(slots=True)
class RegistryEdit:
    store: RegistryStore
    document: RegistryDocument
    original: RegistryRevision
    superseded: list[StoredTurnRecord] = field(default_factory=list)
    """Turn files a lease in this edit replaced; deleted once the registry stops naming them."""

    def restore_stopped(self, source: RegistrySnapshot, names: Sequence[str]) -> tuple[str, ...]:
        restored = self.document.restore_stopped(source, names)
        for name in restored:
            InitialRegistration(
                thread=self.document.require(name), status=self.document.status(name),
            ).prepare_participant(self.store)
        return restored

    def fence_idle_owners(
        self, selected: Sequence[tuple[Thread, int]]
    ) -> tuple[tuple[Thread, int], ...]:
        """Commit all original idle owner fences together, or change none."""
        fenced = tuple(
            (thread, self.document.fence_idle_owner(
                thread, expected_claimed_generation=generation
            ))
            for thread, generation in selected
        )
        self.commit()
        return fenced

    def prepare_native_source(self, owner: RegistryOwner, session_file: str) -> NativeSourcePublication:
        current = owner.require_source_snapshot(self.original.snapshot)
        status = self.document.status(current.name)
        location = RegistrySessionLocation.from_path(session_file, self.store.path.parent)
        return NativeSourcePublication(
            thread=replace(current, session_file=location), status=status,
            previous=current, previous_status=status,
        )

    def lease_turn(
        self,
        current: Thread,
        turn_id: TurnId,
        routing: Routing,
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
            project_resume_due=False,
            turn_record=StoredTurnRecord.begin(self.store.path, identity, turn),
        )
        if current.turn_generation:
            self.superseded.append(StoredTurnRecord.for_identity(self.store.path, current.turn_identity))
        self.document.threads[current.name] = leased
        self.document.last_seen[current.name] = time.time()
        return leased, self.document.owners.generations[current.name]

    def commit(self) -> None:
        if self.document != self.original.document:
            self.store.save_unlocked(self.document)
            self.original = self.store.read_revision_unlocked()
        for record in self.superseded:
            record.retire()
        self.superseded.clear()


@dataclass(frozen=True, slots=True)
class RegistryStore(RegistryLocking, RegistryFile, CarriedDocument[RegistryDocument]):
    json_indent = 2
    cache: RegistryCache = field(init=False, compare=False, repr=False)

    def __reduce__(self) -> tuple[type[Self], tuple[Path]]:
        """Row bindings travel by their store path, never by a process's locks."""
        return type(self), (self.path,)

    def __post_init__(self) -> None:
        # Guard-loss checks must know every private declaration before taking this store's lock.
        PrivateCoordinatorTable.declarations()
        object.__setattr__(self, "cache", RegistryCache.for_path(self.path))

    record_type = RegistryDocument

    def empty(self) -> RegistryDocument:
        return RegistryDocument()

    def _read_unlocked(self) -> RegistryDocument:
        return self.read_revision_unlocked().document

    def _capture_unlocked(self) -> DocumentBytes:
        self.private_guard_unlocked()  # before even a cache hit
        with self.cache.measurement:
            self.cache.counts.requests += 1
        def capture(stale: CachedRegistry) -> DocumentBytes:
            source = super(RegistryStore, self)._capture_unlocked()
            with self.cache.measurement:
                self.cache.counts.captures += 1
            return source
        entry = self.cache.entry
        return entry.at(
            FileRevision.capture(self.path), lambda current: current.source,
            capture,
        )

    def _decode_capture(self, source: DocumentBytes) -> RegistryDocument:
        return self._decode_revision(source).document

    def read_revision_unlocked(self) -> RegistryRevision:
        return self._decode_revision(self._capture_unlocked())

    def _decode_revision(self, source: DocumentBytes) -> RegistryRevision:
        with self.cache.decoding:
            return self.cache.entry.at(
                source.revision, lambda current: current,
                lambda stale: self._cache_revision(stale, source),
            )

    def _cache_revision(self, stale: CachedRegistry, source: DocumentBytes) -> RegistryRevision:
        raw = source.revision.either(
            lambda present: FieldCodec.parse(source.content), lambda: self._encode(self.empty()))
        entry = RegistryRevision(source, stale.decoded(self, raw), raw)
        self.cache.entry = entry
        with self.cache.measurement:
            self.cache.counts.decodes += 1
        return entry

    def snapshot(self) -> RegistrySnapshot:
        source, = self._copy_sources(self)
        return self._decode_snapshot(source)

    def read_turn(self, held: StoredTurnRecord) -> TurnDeclaration:
        """Keep a named turn from retiring during capture, then decode after release."""
        registry, turn = self._copy_sources(self, held.writers)
        current = self._decode_snapshot(registry).current_turn(held)
        return current.read_copied(held, turn)

    def change_turn[Result](
        self, record: StoredTurnRecord, change: Callable[[LockedTurn], Result],
    ) -> Result:
        """Keep retirement excluded while the named turn's own transaction changes it."""
        with self.current_snapshot() as cut:
            named = NamedTurn(record) if cut.snapshot.holds_turn(record.identity) else RetiredTurn()
            return change(named)

    def privacy(self) -> RegistryPrivacy:
        """Capture namespace authority without lending its lock to a consumer."""
        with self.guarded_read():
            return self.private_guard_unlocked()

    def privacy_now(self) -> RegistryPrivacy:
        """Optional reads refuse contention rather than waiting for a writer."""
        with self.read_now():
            return self.private_guard_unlocked()

    def snapshot_now(self) -> RegistrySnapshot:
        with self.read_now():
            source = self._capture_unlocked()
        return self._decode_snapshot(source)

    def snapshot_with_authority(self) -> RegistryCut:
        with self.guarded_read():
            privacy = self.private_guard_unlocked()
            source = self._capture_unlocked()
            revision = source.revision, FileRevision.capture(self.guard_path)
        return RegistryCut(self, self._decode_snapshot(source), privacy, revision)

    def current_snapshot(self) -> AbstractContextManager[RegistryCut]:
        """Decode outside the guard, then retain a current cut for a bounded write.

        A change before the write begins recaptures only this read. The caller has not
        received a decision or performed any side effect yet.
        """
        return RegistryCut.captured(self.snapshot_with_authority)

    def snapshot_pair[Peer](self, other: LockedStore[Peer]) -> RegistryPairCut[Peer]:
        """One registry authority and related document, decoded after their capture."""
        with self._capturing_sources(self, other) as (registry, peer):
            privacy = self.private_guard_unlocked()
            revision = registry.revision, FileRevision.capture(self.guard_path)
        return RegistryPairCut(
            self, self._decode_snapshot(registry), privacy, revision,
            other.read_copied(peer),
        )

    def current_pair[Peer](self, other: LockedStore[Peer]) -> AbstractContextManager[RegistryPairCut[Peer]]:
        return RegistryPairCut[Peer].captured(partial(self.snapshot_pair, other))

    def snapshot_with_pair[T, U](
        self, first: LockedStore[T], second: LockedStore[U],
    ) -> tuple[RegistrySnapshot, T, U]:
        """Capture related files together; decode none while holding a guard."""
        registry, first_source, second_source = self._copy_sources(self, first, second)
        return (self._decode_snapshot(registry),
                first.read_copied(first_source), second.read_copied(second_source))

    def _decode_snapshot(self, source: DocumentBytes) -> RegistrySnapshot:
        revision = self._decode_revision(source)
        with self.cache.decoding:
            return revision.snapshot

    def read_snapshot_unlocked(self) -> RegistrySnapshot:
        revision = self.read_revision_unlocked()
        # Shared readers may be the first to ask for this revision's snapshot.
        # Use the existing decode mutex for that one construction, not the
        # whole caller's read lifetime.
        with self.cache.decoding:
            return revision.snapshot

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
        """Read an entry from the same retained revision as snapshots and edits.

        ThreadRows decodes only the requested declaration. The file and guard
        observations belong to this same locked read, never an earlier stat.
        """
        with self.guarded_read():
            source = self._capture_unlocked()
            revision = source.revision, FileRevision.capture(self.guard_path)
        return RegistryEntryRevision(revision, self._decode_revision(source).document.entry(name))

    def _encode(self, value: RegistryDocument) -> dict:
        return FieldCodec.encode(value)

    def _bind_thread(self, thread: Thread) -> Thread:
        record = thread.turn_record.bound_to(self.path, thread.turn_identity)
        session = thread.session_file.bound_to(self.path.parent)
        if record is thread.turn_record and session is thread.session_file:
            return thread
        return replace(thread, turn_record=record, session_file=session)

    def _decode(self, data: object) -> RegistryDocument:
        document = self.record_type.from_wire(data)
        document.threads.bind(self._bind_thread)
        return document

    @contextmanager
    def editing(self) -> Iterator[RegistryEdit]:
        with self.guarded_write():
            original = self.read_revision_unlocked()
            original.document.purpose.require_write()
            yield RegistryEdit(self, original.document.copy(), original)

    def retire_processes(self) -> None:
        with self.editing() as edit:
            edit.document.retire_processes()
            edit.commit()

    def mark_historical(self) -> None:
        """Explicit archive creation/carry seals this declaration, idempotently."""
        with self.guarded_write():
            original = self.read_revision_unlocked()
            document = replace(original.document, purpose=HistoricalRootPurpose())
            RegistryEdit(self, document, original).commit()

    def save_unlocked(self, document: RegistryDocument) -> None:
        self.read_revision_unlocked().document.purpose.require_write()
        document.threads.bind(self._bind_thread)
        raw = self._encode(document)
        self._write_unlocked(json.dumps(raw, indent=2))
        # Still under the exclusive lock: the written file is this document.
        source = DocumentBytes.capture(self.path)
        source.revision.require(FileNotFoundError(self.path))
        self.cache.entry = RegistryRevision(source, document.copy(), raw)

    def _write_unlocked(self, text: str) -> None:
        # The two-file private guard protocol cannot use A8's rollback writer.
        self.cache.entry = UncachedRegistry()
        self.private_guard_unlocked().write(self.path, text)

    def private_guard_unlocked(self) -> RegistryPrivacy:
        """Validate registry authority before even a cached document read.

        The guard owns its native namespace. Native runtime tables without
        that guard are incomplete state, never a new namespace to initialize.
        """
        # The root is keyed by which directory it is, never by its mtime: every entry created or
        # removed under it, such as a coordinator transaction's journal, moves the mtime, and the
        # entries this guard reads are keyed by their own revisions below.
        key = (
            _link_revision(self.path.parent).file_identity(),
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

            with CoordinationStore.opened_reader(coordinator) as db:
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
