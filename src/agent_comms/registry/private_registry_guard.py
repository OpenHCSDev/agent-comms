"""Default-off private registry durability gate; never a public wire acknowledgement.

This two-slot file owns the native namespace and the registry's committed
bytes. It is read only while the registry's process lock is held. A corrupt
slot, or a pending slot whose replacement may be visible, is deliberately NOT
repaired or skipped: an atomic replacement may already be visible even when
its directory fsync failed.

One pending state is decided rather than refused: an abandoned write. Its
writer died or failed before renaming its replacement into place, so the
registry still holds exactly the previous commit's bytes. A rename that
happened shows the pending digest instead (fsynced or not), and one that never
happened cannot appear later; the previous commit is the authority, and the
next write reuses the abandoned slot.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import NoReturn

from agent_comms.coordination.cohort_schema import AcceptanceFrontier, PrivateCoordinatorSchema
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import PublicationActivationBlocked
from agent_comms.coordination.private_runtime_schema import PrivateCoordinatorTable
from agent_comms.coordination.schema import CoordinatorTable
from agent_comms.coordination.tables.participants import Participants
from agent_comms.coordination.tables.response_schema import ResponseTable
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import Platform
from agent_comms.execution.input_root_authority import InputRootAuthority
from agent_comms.execution.native_input_owner import NativeOwnerRegistry, RegistryOwner
from agent_comms.execution.native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from agent_comms.execution.private_nk_entrypoint import (
    PrivateNkLaunch,
    RootLaunch,
    SharedRootLaunch,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import AcpSessionId, RootId, ThreadLookup
from agent_comms.registry.notification_selection_read import NotificationSelectionRead
from agent_comms.registry.privacy_reads import CursorMetadataRead, NativeAnswerRead
from agent_comms.registry.registry_tables import RegistryRevisionMark, RegistryRowStore
from agent_comms.registry.thread_identity import ClaimedIdentity
from agent_comms.storage.locked_store import RollbackRetained, WireWakeFile
from agent_comms.storage.private_path import FileObservation, PrivateFileRole
from agent_comms.storage.typed_table import TableSchema
from agent_comms.transport.attachment_scope import CursorScope, LiveCursorScope, NoCursorScope


_MAGIC = b"ACRG"


_VERSION = 1


_PENDING = 1


_COMMITTED = 2


_MAX_SEQ = (1 << 63) - 1




_HEADER = struct.Struct("!4sB16sQB32s")


_SIZE = _HEADER.size + 32


_EMPTY = hashlib.sha256(b"absent\0").digest()


def _reject(detail: str) -> NoReturn:
    raise RelationViolationError(f"Private registry guard {detail}")


def registry_digest(path: Path) -> bytes:
    """The committed registry revision this guard vouches for, read from its row store: one
    indexed read, never a hash of every row. No committed rows is the empty registry."""
    return revision_digest(RegistryRowStore(path.parent).mark())


def revision_digest(mark: RegistryRevisionMark) -> bytes:
    return (_EMPTY if not mark.revision
            else hashlib.sha256(b"rows\0" + mark.revision.to_bytes(8, "big")).digest())


def _record(root_id: bytes, seq: int, phase: int, digest: bytes) -> bytes:
    header = _HEADER.pack(_MAGIC, _VERSION, root_id, seq, phase, digest)
    return header + hashlib.sha256(b"agent-comms/private-registry-guard/v1\0" + header).digest()


def _decode(data: bytes, root_id: bytes) -> tuple[int, int, bytes]:
    if len(data) != _SIZE:
        _reject("slot has an invalid length")
    header, checksum = data[: _HEADER.size], data[_HEADER.size :]
    if hashlib.sha256(b"agent-comms/private-registry-guard/v1\0" + header).digest() != checksum:
        _reject("slot checksum is invalid")
    magic, version, stored_root, seq, phase, digest = _HEADER.unpack(header)
    if (
        magic != _MAGIC
        or version != _VERSION
        or stored_root != root_id
        or seq > _MAX_SEQ
        or phase not in (_PENDING, _COMMITTED)
    ):
        _reject("slot is not for this protocol root")
    return seq, phase, digest


class RegistryPrivacy(InputRootAuthority):
    """What a registry root's protocol marker decides, selected by the registry store."""

    def guard_evidence(self, guard: FileObservation, coordinator: FileObservation,
                       registry: RegistryRevisionMark) -> tuple[FileObservation | RegistryRevisionMark, ...]:
        """The observed files this answer was decided from.

        A public root is decided by the guard's absence and the coordinator's schema.
        """
        return guard, coordinator

    def native_answered(self, answers: NativeAnswerRead[MessageReference],
                        references: tuple[MessageReference, ...]) -> frozenset[MessageReference]:
        """Which of these inputs a native transcript answered, read under this root's privacy."""
        return answers.answered_inputs(references)

    @abstractmethod
    def cursor_scope(
        self, snapshot: NativeOwnerRegistry, thread_name: str, session_id: str,
    ) -> CursorScope: ...

    @abstractmethod
    async def cursor_metadata[Update](
        self, publication: CursorMetadataRead[Update], thread_name: str, session_id: AcpSessionId,
    ) -> tuple[Update, ...]: ...

    @abstractmethod
    def write(
        self, registry_path: Path, mark: RegistryRevisionMark,
        commit: Callable[[], RegistryRevisionMark],
    ) -> None:
        """Commit the registry's rows as ``mark``; a private guard brackets the commit."""

    @abstractmethod
    def launch(self, root: Path) -> RootLaunch: ...

    @abstractmethod
    def prepare_native(self, registry_path: Path) -> PrivateRegistryGuard: ...

    @abstractmethod
    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        """Context evidence for every row in the captured assignment window ``w``."""

    @abstractmethod
    def context_entered_sql(self, assignment: str) -> str:
        """Whether a native input committed ``assignment``'s message to its session context."""

    @abstractmethod
    def missing_notification_assignments[Reply](
        self, selection: NotificationSelectionRead[Reply],
    ) -> Reply: ...

    @classmethod
    @abstractmethod
    def coordinator_schema(cls) -> type[TableSchema]:
        """The table family declared by this protocol kind."""

    @classmethod
    def declared_schema_at(cls, registry_path: Path) -> type[TableSchema]:
        """Read a schema declaration without constructing a usable registry capability."""
        return cls.coordinator_schema()

    @classmethod
    @abstractmethod
    def load(cls, registry_path: Path) -> RegistryPrivacy:
        """Load the root's usable privacy capability, including its data commitment checks."""

    @abstractmethod
    def declare_acceptance(self, db: sqlite3.Connection, recipient_lookup: ThreadLookup) -> None:
        """Prepare the new recipient's acceptance state before registry publication."""

    @abstractmethod
    def publication_connection(self, connection: sqlite3.Connection) -> sqlite3.Connection | None:
        """This root's recorded native publications, or no private publication source."""


class PublicRegistry(RegistryPrivacy):

    def declare_acceptance(self, db: sqlite3.Connection, recipient_lookup: ThreadLookup) -> None:
        """Public roots have no native cohort acceptance state."""

    def publication_connection(self, connection: sqlite3.Connection) -> None:
        """Public attached history has no private native publication relations."""

    @classmethod
    def coordinator_schema(cls) -> type[TableSchema]:
        CoordinatorTable.declarations()
        return CoordinatorTable

    @classmethod
    def load(cls, registry_path: Path) -> PublicRegistry:
        return cls()

    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        return (
            "SELECT assignment_id,0 AS triage_inflight,"
            "NULL AS context_committed_at_ms FROM w"
        )

    def context_entered_sql(self, assignment: str) -> str:
        """A public root has no native inputs, so no native context entry."""
        return "0"

    def missing_notification_assignments[Reply](
        self, selection: NotificationSelectionRead[Reply],
    ) -> Reply:
        return selection.read_without_assignments()

    def cursor_scope(
        self, snapshot: NativeOwnerRegistry, thread_name: str, session_id: str,
    ) -> CursorScope:

        return NoCursorScope()

    async def cursor_metadata[Update](
        self, publication: CursorMetadataRead[Update], thread_name: str, session_id: AcpSessionId,
    ) -> tuple[Update, ...]:
        return ()

    def native_answered(self, answers: NativeAnswerRead[MessageReference],
                        references: tuple[MessageReference, ...]) -> frozenset[MessageReference]:
        """A public root has no native inputs, so no native answers."""
        return frozenset()

    @property
    def namespace(self) -> RootId:
        _reject("native namespace requires the original registry guard")

    def write(
        self, registry_path: Path, mark: RegistryRevisionMark,
        commit: Callable[[], RegistryRevisionMark],
    ) -> None:
        commit()

    def launch(self, root: Path) -> RootLaunch:
        return SharedRootLaunch(root)

    def prepare_native(self, registry_path: Path) -> PrivateRegistryGuard:
        guard = PrivateRegistryGuard(registry_path, RootId.mint())
        guard._trusted_root()
        # Install the new private capabilities while the existing public schema
        # still owns opening this file, before publishing the private guard.
        # Participants registered while public declared no acceptance; the new
        # root declares it for each, as registration does for later ones.
        with coordinator.Coordination(str(CoordinationStore.path_in(registry_path.parent))) as store:
            PrivateCoordinatorSchema.install(store)
            with store.session.writing() as db:
                for participant in Participants.select(db):
                    guard.declare_acceptance(db, participant.participant_lookup)
        guard.create_pending()
        guard.commit_initial()
        return guard

    def require_root(self, root_id: RootId, error: Exception) -> None:
        raise error


class PrivateRegistryGuard(RollbackRetained, WireWakeFile, RegistryPrivacy):


    filename = ".registry-owner-guard"

    def guard_evidence(self, guard: FileObservation, coordinator: FileObservation,
                       registry: RegistryRevisionMark) -> tuple[FileObservation | RegistryRevisionMark, ...]:
        """A private root is decided by its guard slots and the registry digest they name."""
        return guard, registry

    def declare_acceptance(self, db: sqlite3.Connection, recipient_lookup: ThreadLookup) -> None:
        AcceptanceFrontier(
            wire_root_id=self.namespace, recipient_lookup=recipient_lookup, through_seq=0,
        ).insert_if_absent(db)

    def publication_connection(self, connection: sqlite3.Connection) -> sqlite3.Connection:

        NativeRuntimeTable.require_installed(connection)
        ResponseTable.require_installed(connection)
        return connection

    @classmethod
    def coordinator_schema(cls) -> type[TableSchema]:
        PrivateCoordinatorTable.declarations()
        return PrivateCoordinatorSchema

    @classmethod
    def declared_schema_at(cls, registry_path: Path) -> type[TableSchema]:
        cls.namespace_at(registry_path)
        return cls.coordinator_schema()

    def prepare_runtime(self) -> None:
        PrivateCoordinatorTable.declarations()
        with coordinator.Coordination(str(CoordinationStore.path_in(self.registry_path.parent))) as store:
            PrivateCoordinatorSchema.install(store)

    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        NativeRuntimeTable.require_installed(connection)
        return NativeRuntimeInput.notification_context_sql()

    def context_entered_sql(self, assignment: str) -> str:
        members = f"({NativeRuntimeInput.source_membership_sql()})"
        return f"EXISTS (SELECT 1 {NativeRuntimeInput.context_entered_from(members, assignment)})"

    def missing_notification_assignments[Reply](
        self, selection: NotificationSelectionRead[Reply],
    ) -> Reply:
        raise PublicationActivationBlocked("Private notification coordinator is missing")

    def __init__(self, registry_path: Path, wire_root_id: RootId):
        self.registry_path = registry_path
        self.path = self.path_in(registry_path.parent)
        self.root_id = bytes.fromhex(wire_root_id)

    @classmethod
    def namespace_at(cls, registry_path: Path) -> RootId:
        """Decode the protocol marker without granting access to registry data."""
        with PrivateFileRole.reading(
            cls.path_in(registry_path.parent),
            RelationViolationError(
                "Private registry guard file is not a fixed owner-only regular file"),
        ) as descriptor:
            raw = os.pread(descriptor, _SIZE, 0)
        if len(raw) != _SIZE:
            _reject("slot has an invalid length")
        _, _, root_id, _, _, _ = _HEADER.unpack(raw[:_HEADER.size])
        _decode(raw, root_id)
        return RootId(root_id.hex())

    @classmethod
    def load(cls, registry_path: Path) -> PrivateRegistryGuard:
        """Read this guard's namespace; both slots and the registry must corroborate it."""
        guard = cls(registry_path, cls.namespace_at(registry_path))
        guard.verify()
        return guard

    @property
    def namespace(self) -> RootId:
        """The existing native protocol namespace, never the SQLite bus identity."""
        return RootId(self.root_id.hex())

    def cursor_scope(
        self, snapshot: NativeOwnerRegistry, thread_name: str, session_id: str,
    ) -> CursorScope:

        try:
            owner = RegistryOwner.capture_local(snapshot, thread_name)
        except RelationViolationError:
            return NoCursorScope()
        return LiveCursorScope(
            session_id=session_id, wire_root_id=self.namespace,
            claimed_identity=ClaimedIdentity(owner.thread.incarnation, owner.claimed_generation),
            owner=owner.thread.require_process(),
        )

    async def cursor_metadata[Update](
        self, publication: CursorMetadataRead[Update], thread_name: str, session_id: AcpSessionId,
    ) -> tuple[Update, ...]:
        return await publication.observed_metadata(thread_name, session_id)

    def launch(self, root: Path) -> RootLaunch:
        return PrivateNkLaunch.installed(root, self.namespace)

    def prepare_native(self, registry_path: Path) -> PrivateRegistryGuard:
        if registry_path != self.registry_path:
            _reject("belongs to another registry")
        self.verify()
        return self

    def require_root(self, root_id: RootId, error: Exception) -> None:
        if root_id != self.namespace:
            raise error

    def _trusted_root(self) -> None:
        if ".." in self.registry_path.parts:
            _reject("has an ambiguous root path")
        root = self.registry_path.parent.absolute()
        cursor = root
        platform = Platform.current()
        while True:
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                _reject("has a missing directory ancestor")
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or not platform.trusted_ancestor_owner(info.st_uid)
                or (platform.ancestor_write_bits(info) and not sticky_root)
            ):
                _reject("has an untrusted directory ancestor")
            if cursor == root and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                _reject("root must be owner-only")
            if cursor == cursor.parent:
                return
            cursor = cursor.parent

    @contextmanager
    def _opened(
        self, opening: Callable[[str | Path, Exception], AbstractContextManager[int]],
    ) -> Iterator[int]:
        """This guard's slots, for as long as the caller's block needs them."""
        self._trusted_root()
        with ExitStack() as scope:
            # Only the open answers "missing"; a short file below is a separate refusal.
            try:
                fd = scope.enter_context(opening(self.path, RelationViolationError(
                    "Private registry guard file is not a fixed owner-only regular file")))
            except FileNotFoundError as error:
                _reject(f"file is missing: {error.__class__.__name__}")
            if os.fstat(fd).st_size != 2 * _SIZE:
                _reject("file is not a fixed owner-only regular file")
            yield fd

    def _slots(self, fd: int) -> tuple[tuple[int, int, bytes], tuple[int, int, bytes]]:
        # Validate BOTH slots even when the first appears to authorize the
        # registry. Falling back after a torn pending slot is unsafe.
        raw = os.pread(fd, 2 * _SIZE, 0)
        first = _decode(raw[:_SIZE], self.root_id)
        second = _decode(raw[_SIZE:], self.root_id)
        low, high = sorted((first[0], second[0]))
        if (low, high) != (0, 0) and high != low + 1:
            _reject("slot sequence is discontinuous or duplicated")
        if (low, high) == (0, 0) and (first != second or first[1] != _COMMITTED):
            _reject("initial slots disagree")
        return first, second

    def _authority(self, fd: int) -> tuple[int, bytes, int]:
        """The committed sequence and digest, and the slot the next write uses."""
        slots = self._slots(fd)
        latest = 0 if slots[0][0] > slots[1][0] else 1
        seq, phase, digest = slots[latest]
        current = registry_digest(self.registry_path)
        if phase == _COMMITTED and seq != 0:
            if digest != current:
                _reject("does not match the registry snapshot")
            return seq, digest, 1 - latest
        previous_seq, previous_phase, previous_digest = slots[1 - latest]
        if (phase == _PENDING and digest != current and previous_phase == _COMMITTED
                and previous_seq != 0 and previous_digest == current):
            # An abandoned write: its replacement never reached the registry.
            return previous_seq, current, latest
        _reject("is pending, not committed")

    @staticmethod
    def _write(fd: int, index: int, value: bytes) -> None:
        written = os.pwrite(fd, value, index * _SIZE)
        if written != len(value):
            _reject("slot write was incomplete")
        os.fsync(fd)

    def verify(self) -> tuple[int, bytes]:
        # A read-only check must not emit IN_CLOSE_WRITE and wake the wire
        # watcher again; private ACP drains verify this guard on every wake.
        with self._opened(PrivateFileRole.reading) as fd:
            seq, digest, _ = self._authority(fd)
            return seq, digest

    def prepare(self, new_digest: bytes) -> tuple[int, int]:
        with self._opened(PrivateFileRole.updating) as fd:
            seq, _, index = self._authority(fd)
            if seq >= _MAX_SEQ:
                _reject("sequence is exhausted")
            self._write(fd, index, _record(self.root_id, seq + 1, _PENDING, new_digest))
            return seq + 1, index

    def commit(self, seq: int, index: int, new_digest: bytes) -> None:
        with self._opened(PrivateFileRole.updating) as fd:
            current = self._slots(fd)[index]
            if current != (seq, _PENDING, new_digest):
                _reject("pending commitment changed")
            if registry_digest(self.registry_path) != new_digest:
                _reject("replacement differs from pending digest")
            self._write(fd, index, _record(self.root_id, seq, _COMMITTED, new_digest))

    def write(
        self, registry_path: Path, mark: RegistryRevisionMark,
        commit: Callable[[], RegistryRevisionMark],
    ) -> None:
        # A pending guard after any failed write MUST remain fail-closed.
        digest = revision_digest(mark)
        sequence, slot = self.prepare(digest)
        if commit() != mark:
            _reject("row commit landed on another revision than the one it prepared")
        self.commit(sequence, slot, digest)

    def create_pending(self) -> None:
        """The registry lock covers explicit native preparation before coordinator setup."""
        self._trusted_root()
        if self.path.exists() or self.path.is_symlink():
            _reject("already exists before initialization")
        digest = registry_digest(self.registry_path)
        Platform.current().sync_directory(self.registry_path.parent)
        with PrivateFileRole.creating(self.path, RelationViolationError(
            "Private registry guard file is not a fixed owner-only regular file"),
        ) as fd:
            if os.pwrite(fd, _record(self.root_id, 0, _COMMITTED, digest) * 2, 0) != 2 * _SIZE:
                _reject("initial slots were incomplete")
            os.fsync(fd)
            Platform.current().sync_directory(self.registry_path.parent)
            self._write(fd, 1, _record(self.root_id, 1, _PENDING, digest))


    def commit_initial(self) -> None:
        with self._opened(PrivateFileRole.updating) as fd:
            first, second = self._slots(fd)
            if first != (0, _COMMITTED, registry_digest(self.registry_path)) or second != (
                1,
                _PENDING,
                first[2],
            ):
                _reject("initial pending record is invalid")
            self._write(fd, 1, _record(self.root_id, 1, _COMMITTED, first[2]))


import agent_comms.coordination.coordinator as coordinator
