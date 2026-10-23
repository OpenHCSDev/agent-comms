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
from typing import Any, ClassVar

from .errors import RelationViolationError
from .field_codec import FieldCodec, NullMember
from .locked_store import DocumentConversion, RegistryFile
from .maintenance_barrier import CarriedDocument
from .native_input_owner import RegistryOwner
from .private_path import FileObservation, FileRevision, MissingFile
from .private_registry_guard import PrivateRegistryGuard, PublicRegistry, RegistryPrivacy
from .registration_change import NativeSourcePublication
from .registry_document import RegistryDocument, RegistryEntry, RegistrySnapshot
from .routing import TurnRouting
from .store_files import SharedLock
from .thread_identity import TurnIdentity
from .thread_text import SavedSessionFile
from .threads import Thread
from .turn_lease import ActiveTurn, RecordedGeneration, StoredTurnRecord
from .wire_metadata import WireMetadata


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
            current.require_process().pid,
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


class RegistryNeedsConversion(RelationViolationError):
    """The installer must explicitly convert this file before a reader uses it."""

    def __init__(self, operation):
        super().__init__(f"Registry needs conversion; run {operation.__qualname__}() during installation")


class RegistryGoalConversion(DocumentConversion):
    """The sole unversioned-to-current registry transformation."""

    source_version = 0
    source_fingerprints = frozenset({
        "3bef5bd6411c7028a599e2a9bd66cb8662ca2a8e24a4ff4f265cb171bd2d5bbd"})

    @classmethod
    def target_version(cls) -> int:
        return RegistryDocument().version

    @classmethod
    def version(cls, raw: object) -> int:
        raw = FieldCodec.decode(dict[str, Any], raw)
        version = FieldCodec.decode(int, raw.get("version", cls.source_version))
        if version not in (cls.source_version, cls.target_version()):
            raise RelationViolationError(f"Unsupported registry version: {version}")
        return version

    @classmethod
    def require_current(cls, raw: dict) -> None:
        if cls.version(raw) != cls.target_version():
            raise RegistryNeedsConversion(RegistryStore.convert_format)

    @classmethod
    def converted(cls, raw: dict) -> dict:
        if cls.version(raw) == cls.target_version():
            return raw
        threads = {name: dict(thread) for name, thread in raw["threads"].items()}
        for thread in threads.values():
            if thread.get("goal") == {}:
                thread["goal"] = None
        return {**raw, "version": cls.target_version(), "threads": threads}


@dataclass(frozen=True, slots=True)
class RegistryStore(RegistryFile, CarriedDocument[RegistryDocument]):
    json_indent = 2
    conversion = RegistryGoalConversion
    cache: RegistryCache = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cache", RegistryCache.for_path(self.path))

    record_type = RegistryDocument

    def empty(self) -> RegistryDocument:
        return RegistryDocument()

    def _read_unlocked(self) -> RegistryDocument:
        return self.read_revision_unlocked().document

    def _current_raw_unlocked(self) -> dict:
        raw = FieldCodec.parse(self.path.read_bytes())
        self.conversion.require_current(raw)
        return raw

    def convert_format(self, *, apply: bool = True) -> bool:
        """Installer-only operation; validate completely before guarded publication.

        The installer stops all old readers before calling this. A dry run
        validates the exact same result without publishing it.
        """
        with self.locked():
            self.private_guard_unlocked()
            if not self.path.exists():
                return False
            raw = FieldCodec.parse(self.path.read_bytes())
            converted = self.conversion.converted(raw)
            self._decode(converted)
            if converted is raw:
                return False
            if apply:
                self._write_unlocked(json.dumps(converted, indent=2))
            return True

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
            lambda present: self._current_raw_unlocked(), lambda: self._encode(self.empty()))
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
        decoded_entry = RegistryDocument.entry_from_wire(self._current_raw_unlocked(), name)
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
            yield RegistryEdit(self, original.document.copy(), original)

    def save_unlocked(self, document: RegistryDocument) -> None:
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
        """Validate the private marker/guard BEFORE even a cached registry read.

        The directory, guard file and registry are one private root. A marker
        without its committed guard (or a guard without a marker) is an
        uncertain migration, never an invitation to bootstrap old metadata.
        """
        marker_path = WireMetadata.path_in(self.path.parent)
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

        marker = WireLog(WireLog.path_in(self.path.parent)).read_metadata_unlocked(required=True)
        if not marker.private:
            if guard_present:
                raise RelationViolationError("Private registry guard marker is absent")
            return PublicRegistry()
        guard = PrivateRegistryGuard(self.path, marker.root_id)
        guard.verify()
        return guard


def _link_revision(path: Path) -> FileObservation:
    """Revision of the directory entry itself, without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return MissingFile()
    return FileRevision.from_stat(info)
