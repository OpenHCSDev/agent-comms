"""A8 registry persistence: cached documents and the existing private durability guard."""

from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import ClassVar, Self

from agent_comms.coordination.database import CoordinationStore, ReadableStore
from agent_comms.coordination.errors import StaleFence
from agent_comms.coordination.private_runtime_schema import PrivateCoordinatorTable
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import NoProcess, Platform
from agent_comms.execution.input_ingress import InputAuthorityCut, InputRegistryStore
from agent_comms.execution.native_input_owner import RegistryOwner
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.turn_lease import (
    ActiveTurn,
    LockedTurn,
    NamedTurn,
    RecordedGeneration,
    RetiredTurn,
    StoredTurnRecord,
    TurnDeclaration,
    TurnRecord,
)
from agent_comms.execution.turn_registry import TurnRegistry
from agent_comms.messaging.routing import Routing
from agent_comms.nominal.field_codec import FieldCodec, NullMember, TurnId
from agent_comms.registry.presence import PresenceCapture, PresenceRevision, PresenceStore
from agent_comms.registry.private_registry_guard import (
    PrivateRegistryGuard,
    PublicRegistry,
    RegistryPrivacy,
)
from agent_comms.registry.registration_change import InitialRegistration, NativeSourcePublication
from agent_comms.registry.registration_reads import RegistryPathRead
from agent_comms.registry.registry_document import (
    HistoricalRootPurpose,
    RegistryDocument,
    RegistryEntry,
    RegistrySnapshot,
)
from agent_comms.registry.registry_tables import (
    RawDocument,
    RegistryCapture,
    RegistryRowRevision,
    RegistryRowStore,
)
from agent_comms.registry.thread_identity import TurnIdentity
from agent_comms.registry.threads import Thread
from agent_comms.storage.locked_store import (
    LockedStore,
    PublishedProperty,
    RegistryFile,
    RootFile,
)
from agent_comms.storage.maintenance_barrier import CarriedDocument
from agent_comms.storage.private_path import FileObservation, FileRevision, MissingFile
from agent_comms.storage.store_files import RegistryLocking
from agent_comms.storage.typed_table import TableSchema


class CachedRegistry(ABC):
    """The registry revision this process last decoded, or none yet."""

    @abstractmethod
    def at[Answer](
        self, revision: RegistryRowRevision, reuse: Callable[[RegistryRevision], Answer],
        read: Callable[[CachedRegistry], Answer],
    ) -> Answer:
        """Reuse this revision's answer, or read its replacement."""

    @abstractmethod
    def decoded(self, store: RegistryStore, source: RegistryCapture) -> RegistryDocument:
        """Decode ``raw``, reusing the threads this revision already decoded."""

    @abstractmethod
    def captured(self, store: RegistryStore) -> RegistryCapture:
        """The committed revision, read as the rows changed since this one."""

    @abstractmethod
    def holds(self, capture: RegistryCapture) -> bool:
        """Whether ``capture`` is this revision's own, so no row was read for it."""


@dataclass(frozen=True)
class UncachedRegistry(NullMember, CachedRegistry):
    def at[Answer](
        self, revision: RegistryRowRevision, reuse: Callable[[RegistryRevision], Answer],
        read: Callable[[CachedRegistry], Answer],
    ) -> Answer:
        return read(self)

    def decoded(self, store: RegistryStore, source: RegistryCapture) -> RegistryDocument:
        return store._decode(source.raw, (RegistryCapture.empty().raw, RegistryDocument()))

    def captured(self, store: RegistryStore) -> RegistryCapture:
        return store.rows.captured(RegistryCapture.empty())

    def holds(self, capture: RegistryCapture) -> bool:
        return False


@dataclass(frozen=True)
class RegistryRevision(CachedRegistry):
    """A committed registry revision: its document and the raw rows it was decoded from.

    The raw rows let the next revision read and decode only the rows that changed.
    An uncommitted registry's rows are the empty document's.
    """

    source: RegistryCapture
    document: RegistryDocument
    presence: PresenceRevision
    """The presence commit laid over the document; heartbeats leave declarations alone."""

    @classmethod
    def presented(cls, store: RegistryStore, source: RegistryCapture,
                  document: RegistryDocument) -> RegistryRevision:
        captured = store.presence.read(store.cache.presence_source)
        store.cache.presence_source = captured
        # Validate against the edit's original key root before copying resets its
        # change baseline. The retained document then owns an independent edit view.
        return cls(source, document.presented(captured).copy(), captured.revision)

    def current(self, store: RegistryStore) -> RegistryRevision:
        """This registry revision with the presence stored now: re-laid, and cached in this
        revision's place, only when presence committed since it was laid."""
        if store.presence.revision() == self.presence:
            return self
        revision = RegistryRevision.presented(store, self.source, self.document)
        store.cache.entry = revision
        return revision

    def at[Answer](
        self, revision: RegistryRowRevision, reuse: Callable[[RegistryRevision], Answer],
        read: Callable[[CachedRegistry], Answer],
    ) -> Answer:
        return reuse(self) if self.source.revision == revision else read(self)

    def decoded(self, store: RegistryStore, source: RegistryCapture) -> RegistryDocument:
        document = store._decode(source.raw, (self.source.raw, self.document))
        return replace(document, last_seen=self.document.last_seen)

    def captured(self, store: RegistryStore) -> RegistryCapture:
        return store.rows.captured(self.source)

    def holds(self, capture: RegistryCapture) -> bool:
        return capture is self.source

    @cached_property
    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot.capture(self.document)


@dataclass(frozen=True, slots=True)
class RegistrySelection:
    """A decoded registry selection whose source stays current during dependent writes."""

    store: RegistryStore = field(repr=False, compare=False)
    revision: tuple[RegistryRowRevision, FileObservation]

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
class RegistryCut(RegistrySelection, InputAuthorityCut[Thread]):
    """The complete decoded registry and its original namespace authority."""

    snapshot: RegistrySnapshot
    privacy: RegistryPrivacy


@dataclass(frozen=True, slots=True)
class RegistryEntriesCut(RegistryCut):
    """A complete cut whose decisions read only these threads' entries.

    Other threads' writes (every live owner's heartbeat) leave such a decision
    intact, so they never stale it; any change to one of these entries does.
    """

    names: frozenset[str]

    @contextmanager
    def current(self) -> Iterator[None]:
        with self.store.guarded_read():
            for name in self.names:
                if self.store.entry_unlocked(name) != self.snapshot.entry(name):
                    raise StaleFence(f"registry entry {name!r} changed after this decision read it")
            yield


@dataclass(frozen=True, slots=True)
class RegistryPairCut[Peer](RegistryCut):
    peer: Peer


@dataclass(frozen=True, slots=True)
class RegistryEntryRevision(RegistrySelection):
    """One thread's entry and the registry revision it was read from, under one lock."""

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
    presence_source: PresenceCapture = field(default_factory=PresenceCapture.empty)
    # Observers wake on the same registry change from several threads; one
    # decodes the new revision and the others take its result.
    decoding: threading.Lock = field(default_factory=threading.Lock)
    # A successful guard check, keyed by the revisions of the files its answer
    # was decided from. Unchanged files give the same answer; failures are never kept.
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
    released: list[Callable[[], None]] = field(default_factory=list)
    """Other stores' writes this edit causes; they run after the registry lock is released, so
    no unit's registry read waits behind a second store's lock."""

    def prepare_native_source(self, owner: RegistryOwner, session_file: str) -> NativeSourcePublication:
        current = owner.require_source_snapshot(self.original.snapshot)
        status = self.document.status(current.name)
        location = RegistrySessionLocation.from_path(session_file, self.store.path.parent)
        return NativeSourcePublication(
            thread=replace(current, session_file=location), status=status,
            previous=current, previous_status=status,
        )

    def restore_stopped(self, source: RegistrySnapshot, names: Sequence[str]) -> tuple[str, ...]:
        restored = self.document.restore_stopped(source, names)
        for name in restored:
            InitialRegistration(
                thread=self.document.require(name), status=self.document.status(name),
            ).prepare_participant(self.store)
        return restored

    def fence_restart_owners(
        self, selected: Sequence[tuple[Thread, int]]
    ) -> tuple[tuple[Thread, int], ...]:
        """Commit all original restart fences together, or change none."""
        fenced = tuple(
            (thread, self.document.fence_restart_owner(
                thread, expected_claimed_generation=generation
            ))
            for thread, generation in selected
        )
        self.commit()
        return fenced

    def lease_turn[Owner: Thread](
        self,
        current: Owner,
        turn_id: TurnId,
        routing: Routing,
    ) -> tuple[Owner, int]:
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
            turn_record=StoredTurnRecord.begin(self.store.path, identity, turn, registry_type=RegistryStore),
        )
        if current.turn_generation:
            self.superseded.append(StoredTurnRecord.for_identity(self.store.path, current.turn_identity, registry_type=RegistryStore))
        self.document.threads[current.name] = leased
        self.document.last_seen[current.name] = time.time()
        return leased, self.document.owners.generations[current.name]

    def commit(self) -> None:
        # Presence rows before the registry, and none dropped: any revision a reader captured,
        # however long before it reads presence, names only threads that have a row.
        if self.document.last_seen != self.original.document.last_seen:
            self.store.presence.set_rows(self.document.last_seen, self.original.document.last_seen)
        if self.document != self.original.document:
            self.store.save_unlocked(self.document)
            self.original = self.store.read_revision_unlocked()
        for record in self.superseded:
            record.retire()
        self.superseded.clear()


@dataclass(frozen=True, slots=True)
class RegistryStore(
    RegistryLocking,
    RegistryFile,
    CarriedDocument[RegistryDocument],
    TurnRegistry[StoredTurnRecord, TurnDeclaration, LockedTurn],
    InputRegistryStore[Thread],
    RegistryPathRead,
    declared_name=None,
):
    cache: RegistryCache = field(init=False, compare=False, repr=False)
    presence: PresenceStore = field(init=False, compare=False, repr=False)
    rows: RegistryRowStore = field(init=False, compare=False, repr=False)

    def __reduce__(self) -> tuple[type[Self], tuple[Path]]:
        """Row bindings travel by their store path, never by a process's locks."""
        return type(self), (self.path,)

    def __post_init__(self) -> None:
        # Guard-loss checks must know every private declaration before taking this store's lock.
        PrivateCoordinatorTable.declarations()
        object.__setattr__(self, "cache", RegistryCache.for_path(self.path))
        object.__setattr__(self, "presence", PresenceStore(self.path.parent))
        object.__setattr__(self, "rows", RegistryRowStore(self.path.parent))

    record_type = RegistryDocument

    def stored_files(self) -> tuple[Path, ...]:
        """The registry and presence files selected together by installer backups."""
        return self.path, self.presence.path

    def retain(self, destination: Path) -> RegistryRevision:
        """Retain a consistent registry for history, then seal the copy against live writes.

        SQLite backup includes committed WAL rows. Presence is copied with its own file owner;
        neither operation retires the source's participants or copies live execution grants.
        """
        copied = type(self).at(destination)
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.guarded_read():
            self.copy_file(self.path, copied.path)
            Platform.current().sync_file(copied.path)
            if self.presence.path.exists():
                self.presence.copy_file(self.presence.path, copied.presence.path)
                Platform.current().sync_file(copied.presence.path)
        copied.mark_historical()
        return copied.read_revision()

    def has_revision(self, revision: RegistryRowRevision) -> bool:
        """Check a retained content revision under the same guard used to capture it."""
        with self.guarded_read():
            return self._capture_unlocked().revision == revision

    def empty(self) -> RegistryDocument:
        return RegistryDocument()

    def _read_unlocked(self) -> RegistryDocument:
        return self.read_revision_unlocked().document

    def _capture_unlocked(self) -> RegistryCapture:
        """The committed revision: the cached one's own capture while nothing committed since,
        otherwise the cached revision with only the rows changed since it applied."""
        self.private_guard_unlocked()  # before even a cache hit
        entry = self.cache.entry
        source = entry.captured(self)
        with self.cache.measurement:
            self.cache.counts.requests += 1
            if not entry.holds(source):
                self.cache.counts.captures += 1
        return source

    def _decode_capture(self, source: RegistryCapture) -> RegistryDocument:
        return self._decode_revision(source).document

    def read_revision(self) -> RegistryRevision:
        """Retain the captured document and its source without imposing live snapshot rules."""
        source, = self._copy_sources(self)
        return self._decode_revision(source)

    def read_revision_unlocked(self) -> RegistryRevision:
        return self._decode_revision(self._capture_unlocked())

    def _decode_revision(self, source: RegistryCapture) -> RegistryRevision:
        with self.cache.decoding:
            return self.cache.entry.at(
                source.revision, lambda current: current,
                lambda stale: self._cache_revision(stale, source),
            ).current(self)

    def _cache_revision(self, stale: CachedRegistry, source: RegistryCapture) -> RegistryRevision:
        entry = RegistryRevision.presented(self, source, stale.decoded(self, source))
        self.cache.entry = entry
        with self.cache.measurement:
            self.cache.counts.decodes += 1
        return entry

    def snapshot(self) -> RegistrySnapshot:
        source, = self._copy_sources(self)
        return self._decode_snapshot(source)

    def read_turns(self, held: tuple[StoredTurnRecord, ...]) -> tuple[TurnDeclaration, ...]:
        """Keep named turns from retiring during capture, then decode after release."""
        registry, *turns = self._copy_sources(self, *(record.writers for record in held))
        decoded = self._decode_snapshot(registry)
        return tuple(decoded.current_turn(record).read_copied(record, turn)
                     for record, turn in zip(held, turns, strict=True))

    def change_turn[Result](
        self, record: StoredTurnRecord, change: Callable[[LockedTurn], Result],
    ) -> Result:
        """Keep retirement excluded while the named turn's own transaction changes it."""
        with self.current_snapshot() as cut:
            named = NamedTurn(record) if cut.snapshot.holds_turn(record.identity) else RetiredTurn()
            return change(named)

    def prepare_native_root(self) -> PrivateRegistryGuard:
        """Commit this store's namespace before preparing runtime tables outside its writer."""
        with self.guarded_write():
            self.read_revision_unlocked().document.purpose.require_write()
            guard = self.private_guard_unlocked().prepare_native(self.path)
        guard.prepare_runtime()
        return guard

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
        return RegistryCut(self, revision, self._decode_snapshot(source), privacy)

    def entries_cut(self, names: frozenset[str]) -> RegistryEntriesCut:
        cut = self.snapshot_with_authority()
        return RegistryEntriesCut(self, cut.revision, cut.snapshot, cut.privacy, names)

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
            self, revision, self._decode_snapshot(registry), privacy,
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

    def _decode_snapshot(self, source: RegistryCapture) -> RegistrySnapshot:
        revision = self._decode_revision(source)
        with self.cache.decoding:
            return revision.snapshot

    @property
    def guard_path(self) -> Path:
        return PrivateRegistryGuard.path_in(self.path.parent)

    @property
    def written(self) -> tuple[Path, Path]:
        """The files every commit writes together, as ``revision_unlocked`` reads them."""
        return self.path, self.guard_path

    @property
    def cut_paths(self) -> tuple[Path, ...]:
        """The files one hold of the registry lock keeps consistent: every write replaces
        ``written`` together, and a turn claim publishes its turn file in the same
        transaction. Read under that lock, so a claim cannot fall between them."""
        return (self.path, *self.companion_paths)

    @property
    def companion_paths(self) -> tuple[Path, ...]:
        return tuple(self.companion_files)

    @property
    def companion_files(self) -> dict[Path, type[RootFile]]:
        """Retain each companion with its owner's copying behavior."""
        return {self.presence.path: type(self.presence), self.guard_path: PrivateRegistryGuard,
                **dict.fromkeys(sorted(StoredTurnRecord.matching_in(self.path.parent)), StoredTurnRecord)}

    def revision_unlocked(self) -> tuple[RegistryRowRevision, FileObservation]:
        """The database instance, committed counter and guard that brackets every commit."""
        return self.rows.revision(), FileRevision.capture(self.guard_path)

    def read_entry(self, name: str) -> RegistryEntryRevision:
        """Read an entry from the same retained revision as snapshots and edits.

        ThreadRows decodes only the requested declaration. The file and guard
        observations belong to this same locked read, never an earlier stat.
        """
        with self.guarded_read():
            source = self._capture_unlocked()
            revision = source.revision, FileRevision.capture(self.guard_path)
        return RegistryEntryRevision(self, revision, self._decode_revision(source).document.entry(name))

    def entry_unlocked(self, name: str) -> RegistryEntry:
        """This thread's current entry; the caller holds the registry guard."""
        return self._decode_revision(self._capture_unlocked()).document.entry(name)

    def current_entry(self, name: str) -> AbstractContextManager[RegistryEntryRevision]:
        """Retain one current participant without materializing unrelated declarations."""
        return RegistryEntryRevision.captured(partial(self.read_entry, name))

    def observed_entry(self, name: str) -> RegistryEntry:
        """Observe one atomic declaration without a publication or execution permission.

        History's LockFreeObserver may see either side of an in-flight replacement;
        it can settle only an intent this complete declaration already shows.
        Namespace/privacy validation still precedes even a cached capture.
        """
        revision = self.revision_unlocked()
        source = self._capture_unlocked()
        if source.revision != revision[0] or self.revision_unlocked() != revision:
            raise StaleFence("Registry changed during history observation; read history again")
        return self.read_copied(source).entry(name)

    def _encode(self, value: RegistryDocument) -> dict:
        return FieldCodec.encode(value)

    def _bind_thread(self, thread: Thread) -> Thread:
        record = thread.turn_record.bound_to(self.path, thread.turn_identity, registry_type=RegistryStore)
        session = thread.session_file.bound_to(self.path.parent)
        if record is thread.turn_record and session is thread.session_file:
            return thread
        return replace(thread, turn_record=record, session_file=session)

    def _decode(self, data: object, earlier: tuple[Mapping[str, object], RegistryDocument] | None = None) -> RegistryDocument:
        document = self.record_type.from_wire(data, earlier)
        document.threads.bind(self._bind_thread)
        return document

    @contextmanager
    def editing(self) -> Iterator[RegistryEdit]:
        with self.guarded_write():
            original = self.read_revision_unlocked()
            original.document.purpose.require_write()
            edit = RegistryEdit(self, original.document.copy(), original)
            yield edit
        for write in edit.released:
            write()

    def retire_processes(self) -> None:
        """Withdraw the source's process and turn references from a runtime copy.

        This changes those owned fields without decoding unrelated thread state.
        """
        threads = FieldCodec.record_key(RegistryDocument, "threads")
        self.rewrite_stored(lambda raw: {
            **raw, threads: {name: self.retired_row(row) for name, row in raw[threads].items()}})

    def stored(self) -> dict:
        """A copy of the committed rows in their stored wire form, before any decoding."""
        with self.guarded_read():
            captured = self._capture_unlocked()
        return FieldCodec.parse(FieldCodec.compact(captured.wire_document()))

    def rewrite_stored(self, change: Callable[[dict], dict]) -> None:
        """Change the stored rows under the exclusive guard without decoding thread state.

        ``change`` gets its own copy. An unchanged result keeps the committed revision.
        """
        with self.guarded_write():
            entry = self.cache.entry
            original = self._capture_unlocked()
            stored = original.wire_document()
            raw = change(FieldCodec.parse(FieldCodec.compact(stored)))
            if raw != stored:
                self._commit_unlocked(raw, original)
                # The earlier revision stays cached: the next read applies only the rows this
                # commit changed, and decodes them then, so a form the decoder refuses is refused
                # by its reader, never here.
                self.cache.entry = entry

    @staticmethod
    def retired_row(row: dict) -> dict:
        process = FieldCodec.record_key(Thread, "process_identity")
        turn = FieldCodec.record_key(Thread, "turn_record")
        return {**row, process: FieldCodec.encode(NoProcess()),
                turn: FieldCodec.encode(FieldCodec.decode(TurnRecord, row[turn]).released())}

    def mark_historical(self) -> None:
        """Explicit archive creation/carry seals this declaration, idempotently."""
        with self.guarded_write():
            original = self.read_revision_unlocked()
            document = replace(original.document, purpose=HistoricalRootPurpose())
            RegistryEdit(self, document, original).commit()

    def save_unlocked(self, document: RegistryDocument) -> None:
        original = self.read_revision_unlocked()
        original.document.purpose.require_write()
        document.require_valid_rows(original.document)
        document.threads.bind(self._bind_thread)
        raw = original.source.encoded(document, original.document)
        source = self._commit_unlocked(raw, original.source)
        # Still under the exclusive lock: the committed revision is this document.
        self.cache.entry = RegistryRevision.presented(self, source, document)

    def _publish_unlocked(self, document: RegistryDocument) -> None:
        self.save_unlocked(document)

    def _commit_unlocked(self, raw: RawDocument, previous: RegistryCapture) -> RegistryCapture:
        """Commit the rows of ``raw`` that differ from ``previous``, bracketed by the guard."""
        mark = previous.mark.successor()
        self.cache.entry = UncachedRegistry()
        self.private_guard_unlocked().write(self.path, mark, lambda: self.rows.commit(raw, previous))
        return RegistryCapture(self.rows.revision(), raw)

    def private_guard_unlocked(self) -> RegistryPrivacy:
        """Validate registry authority before even a cached document read.

        The guard owns its native namespace. Native runtime tables without
        that guard are incomplete state, never a new namespace to initialize.
        """
        # The root is keyed by which directory it is, never by its mtime: every entry created or
        # removed under it, such as a coordinator transaction's journal, moves the mtime, and the
        # entries this guard reads are keyed by their own revisions below.
        # Observed before the check, so a change during it moves the key and forces a recheck.
        # Each answer keys on the files it was decided from: the coordinator commits on every
        # coordination write, and a private root never reads it, so keying a private answer
        # on it re-hashed the whole registry on every coordination write.
        revision = self.rows.revision()
        root = (_link_revision(self.path.parent).file_identity(), revision.identity)
        observed = (
            _link_revision(self.guard_path),
            _link_revision(CoordinationStore.path_in(self.path.parent)),
            # The committed row revision, never the database's stat: a WAL commit leaves the
            # file untouched, so a stat key would keep a verdict across a commit.
            revision.mark,
        )
        if (checked := self.cache.guard) is not None:
            key, guard = checked
            if key == (root, *guard.guard_evidence(*observed)):
                return guard
        guard = self._check_guard_unlocked()
        self.cache.guard = (root, *guard.guard_evidence(*observed)), guard
        return guard

    @PublishedProperty
    def coordinator_reader(self) -> ReadableStore:
        return ReadableStore(CoordinationStore.path_in(self.path.parent))

    def coordinator_schema(self) -> type[TableSchema]:
        """Read schema declarations without loading executable registry state.

        A target installer needs these declarations before its registry carry can run.
        The protocol marker's shape is stable; it grants no participant or launch access.
        """
        with self.guarded_read():
            return self._privacy_declaration_unlocked().declared_schema_at(self.path)

    def _privacy_declaration_unlocked(self) -> type[RegistryPrivacy]:
        if self.guard_path.exists() or self.guard_path.is_symlink():
            return PrivateRegistryGuard
        coordinator = CoordinationStore.path_in(self.path.parent)
        if coordinator.exists() or coordinator.is_symlink():
            with self.coordinator_reader.reader_snapshot() as db:
                if PrivateCoordinatorTable.stored_schema(db):
                    raise RelationViolationError("Native runtime has no registry authority")
        return PublicRegistry

    def _check_guard_unlocked(self) -> RegistryPrivacy:
        return self._privacy_declaration_unlocked().load(self.path)


def _link_revision(path: Path) -> FileObservation:
    """Revision of the directory entry itself, without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return MissingFile()
    return FileRevision.from_stat(info)
