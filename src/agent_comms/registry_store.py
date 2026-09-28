"""A8 registry persistence: cached documents and the existing private durability guard."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from .errors import RelationViolationError
from .field_codec import FieldCodec
from .locked_store import LockedStore
from .private_registry_guard import PrivateRegistryGuard
from .registry_document import RegistryDocument
from .store_files import file_revision


@dataclass(slots=True)
class RegistryCache:
    revision: tuple[int, int, int, int] | None = None
    document: RegistryDocument = field(default_factory=RegistryDocument)


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
    cache: RegistryCache = field(default_factory=RegistryCache, compare=False)

    @property
    def record_type(self) -> type[RegistryDocument]:
        return RegistryDocument

    def empty(self) -> RegistryDocument:
        return RegistryDocument()

    def _read_unlocked(self) -> RegistryDocument:
        self.private_guard_unlocked()  # before even a cache hit
        revision = file_revision(self.path)
        if revision is not None and revision == self.cache.revision:
            return self.cache.document
        self.cache.revision = None
        document = (
            self._decode(json.loads(self.path.read_text()))
            if revision is not None
            else self.empty()
        )
        self.cache.document = document
        self.cache.revision = revision
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
        self.cache.document = document.copy()

    def _write_unlocked(self, text: str) -> None:
        # The two-file private guard protocol cannot use A8's rollback writer:
        # a pending guard after any failed write MUST remain fail-closed.
        from . import store_files

        self.cache.revision = None
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
