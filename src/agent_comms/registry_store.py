"""A8 registry persistence: cached documents and the existing private durability guard."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from .errors import RelationViolationError
from .field_codec import FieldCodec
from .locked_store import LockedStore
from .private_path import FileRevision
from .private_registry_guard import PrivateRegistryGuard, PublicRegistry, RegistryPrivacy
from .registry_document import RegistryDocument, RegistryEntry, RegistrySnapshot
from .registration_change import NativeSourcePublication
from .native_input_owner import RegistryOwner
from .routing import TurnRouting
from .store_files import LockMode, SharedLock, StoreLock
from .thread_identity import TurnIdentity
from .threads import Thread
from .turn_lease import ActiveTurn, StoredTurnRecord


@dataclass(frozen=True)
class RegistryRevision:
    """A registry file revision: its document and the raw rows it was decoded from.

    The raw rows let the next revision decode only the threads that changed.
    """

    revision: FileRevision | None
    raw: dict
    store: RegistryStore = field(repr=False, compare=False)
    earlier: tuple[dict, RegistryDocument] | None = field(default=None, repr=False)

    @cached_property
    def document(self) -> RegistryDocument:
        if self.revision is None:
            return self.store.empty()
        return self.store._bind_document(RegistryDocument.from_wire(self.raw, self.earlier))

    def successor(self, revision: FileRevision | None, raw: dict) -> RegistryRevision:
        # Retain only an already decoded predecessor, not a chain of revisions.
        earlier = ((self.raw, self.__dict__["document"])
                   if "document" in self.__dict__ else None)
        return RegistryRevision(revision, raw, self.store, earlier)

    @classmethod
    def written(cls, revision: FileRevision, raw: dict, store: RegistryStore,
                document: RegistryDocument) -> RegistryRevision:
        result = cls(revision, raw, store)
        result.__dict__["document"] = document.copy()
        return result

    def entry(self, name: str) -> RegistryEntry:
        if "document" in self.__dict__:
            return self.document.entry(name)
        entry = RegistryDocument.entry_from_wire(self.raw, name)
        return replace(entry, thread=self.store._bind_thread(entry.thread))

    @cached_property
    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot.capture(self.document)


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
    original: RegistryRevision

    def prepare_native_source(self, owner: RegistryOwner, session_file: str) -> NativeSourcePublication:
        current = owner.require_source_snapshot(self.original.snapshot)
        status = self.document.statuses[current.name]
        return NativeSourcePublication(
            thread=replace(current, session_file=session_file), status=status,
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
        if self.document != self.original.document:
            self.store.save_unlocked(self.document)
            self.original = self.store.read_revision_unlocked()


class RegistryGoalConversion:
    """The unversioned registry goal transition, never an alternate decoder."""

    source_version = 0
    target_version = RegistryDocument().version

    @classmethod
    def converted(cls, raw: dict) -> dict:
        threads = {name: dict(thread) for name, thread in raw.get("threads", {}).items()}
        for thread in threads.values():
            if thread.get("goal") == {}:
                thread["goal"] = None
        return {**raw, "version": cls.target_version, "threads": threads}


class RegistryStoreLock(StoreLock):
    @classmethod
    @contextmanager
    def hold(cls, path: Path, descriptor: int, mode: LockMode):
        RegistryStore(path)._prepare_unlocked(mode)
        yield cls(descriptor)


@dataclass(frozen=True, slots=True)
class RegistryStore(LockedStore[RegistryDocument], lock_rank=2):
    json_indent = 2
    lock_type: ClassVar[type[StoreLock]] = RegistryStoreLock
    conversion: ClassVar[type[RegistryGoalConversion]] = RegistryGoalConversion
    cache: RegistryCache = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cache", RegistryCache.for_path(self.path))

    @property
    def record_type(self) -> type[RegistryDocument]:
        return RegistryDocument

    def empty(self) -> RegistryDocument:
        return RegistryDocument()

    def _read_unlocked(self) -> RegistryDocument:
        return self.read_revision_unlocked().document

    def _prepare_unlocked(self, mode: LockMode) -> None:
        revision = self._raw_revision_unlocked()
        if revision.revision is None:
            return
        version = FieldCodec.decode(int, revision.raw.get("version", 0))
        if version == self.conversion.target_version:
            return
        if version != self.conversion.source_version:
            raise RelationViolationError(f"Unsupported registry version: {version}")
        mode.require_writer()
        # Decode completely before publication; malformed goals never become NoGoal.
        document = self._decode(self.conversion.converted(revision.raw))
        self.save_unlocked(document)

    def read_revision_unlocked(self) -> RegistryRevision:
        revision = self._raw_revision_unlocked()
        with self.cache.decoding:
            revision.document
        return revision

    def _raw_revision_unlocked(self) -> RegistryRevision:
        self.private_guard_unlocked()  # before even a cache hit
        revision = FileRevision.capture(self.path)
        entry = self.cache.entry
        if entry is not None and entry.revision == revision:
            return entry
        with self.cache.decoding:
            entry = self.cache.entry
            if entry is not None and entry.revision == revision:
                return entry
            raw = {} if revision is None else json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise RelationViolationError("Registry document must be an object")
            entry = (RegistryRevision(revision, raw, self) if entry is None
                     else entry.successor(revision, raw))
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
            source = self._raw_revision_unlocked()
            revision = self.revision_unlocked()
            if source.revision is None:
                return RegistryEntryRevision(revision, self.empty().entry(name))
            with self.cache.decoding:
                return RegistryEntryRevision(revision, source.entry(name))

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
        if (revision := FileRevision.capture(self.path)) is not None:
            self.cache.entry = RegistryRevision.written(revision, raw, self, document)

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
