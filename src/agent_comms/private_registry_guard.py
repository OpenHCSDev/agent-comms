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
import json
import os
import sqlite3
import stat
import struct
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

from . import store_files
from .child_process import Platform
from .coordination_database import CoordinationStore
from .coordinator import Coordination
from .errors import RelationViolationError
from .field_codec import FieldCodec, Id128Text, MalformedRecord, WireName
from .locked_store import DurableFile, WireWakeFile
from .native_runtime_input import NativeRuntimeInput, NativeRuntimeTable

if TYPE_CHECKING:
    from .acp_extension import CursorAdvancedUpdate, CursorScope
    from .bus_publication import Delivered
    from .cursor_publication import CursorPublication
    from .notification_assignment import NotificationAssignmentRead
    from .private_nk_entrypoint import RootLaunch
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .typed_table import TableSchema

_MAGIC = b"ACRG"
_VERSION = 1
_PENDING = 1
_COMMITTED = 2
_MAX_SEQ = (1 << 63) - 1
_MAX_REGISTRY = 32 << 20
_HEADER = struct.Struct("!4sB16sQB32s")
_SIZE = _HEADER.size + 32
_EMPTY = hashlib.sha256(b"absent\0").digest()


def _reject(detail: str) -> NoReturn:
    raise RelationViolationError(f"Private registry guard {detail}")


def registry_digest(path: Path) -> bytes:
    """Hash the exact owned registry bytes, distinguishing absence from empty."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _EMPTY
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > _MAX_REGISTRY
        ):
            _reject("registry is not a bounded owner-only regular file")
        digest = hashlib.sha256(b"present\0")
        total = 0
        while data := os.read(fd, 65536):
            total += len(data)
            if total > _MAX_REGISTRY:
                _reject("registry grew beyond the bounded evidence read")
            digest.update(data)
        if total != info.st_size:
            _reject("registry changed during its evidence read")
        return digest.digest()
    finally:
        os.close(fd)


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


class RegistryPrivacy(ABC):
    """What a registry root's protocol marker decides, selected by the registry store."""

    @staticmethod
    def capture(root: Path) -> RegistryPrivacy:
        """Read original privacy authority under its registry's existing lock."""
        from .registry_store import RegistryStore
        from .store_files import SharedLock

        registry = RegistryStore.at(root)
        with registry.locked(mode=SharedLock()):
            return registry.private_guard_unlocked()

    @abstractmethod
    def cursor_scope(
        self, snapshot: RegistrySnapshot, thread_name: str, session_id: str,
    ) -> CursorScope: ...

    @abstractmethod
    async def cursor_metadata(
        self, publication: CursorPublication, thread_name: str, session_id: str,
    ) -> tuple[CursorAdvancedUpdate, ...]: ...

    @property
    @abstractmethod
    def namespace(self) -> str:
        """Require the original native namespace; a public registry has none."""

    @abstractmethod
    def write(self, registry_path: Path, text: str) -> None: ...

    @abstractmethod
    def register_restored(self, threads: Iterable[Thread]) -> None: ...

    @abstractmethod
    def launch(self, root: Path) -> RootLaunch: ...

    @abstractmethod
    def prepare_native(self, registry_path: Path) -> PrivateRegistryGuard: ...

    @abstractmethod
    def require_root(self, root_id: str, error: Exception) -> None: ...

    @abstractmethod
    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        """Context evidence for every row in the captured assignment window ``w``."""

    @abstractmethod
    def missing_notification_assignments(
        self, sources: Sequence[Delivered],
    ) -> NotificationAssignmentRead: ...

    @abstractmethod
    def coordinator_schema(self) -> type[TableSchema]:
        """The complete schema this root's authority requires during reconciliation."""



class PublicRegistry(RegistryPrivacy):
    def coordinator_schema(self) -> type[TableSchema]:
        from .coordination_schema import CoordinatorTable

        CoordinatorTable.declarations()
        return CoordinatorTable

    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        return (
            "SELECT assignment_id,0 AS triage_inflight,"
            "NULL AS context_committed_at_ms FROM w"
        )

    def missing_notification_assignments(
        self, sources: Sequence[Delivered],
    ) -> NotificationAssignmentRead:
        from .notification_assignment import NotificationAssignmentRead

        return NotificationAssignmentRead(tuple(sources), ())

    def cursor_scope(
        self, snapshot: RegistrySnapshot, thread_name: str, session_id: str,
    ) -> CursorScope:
        from .acp_extension import NoCursorScope

        return NoCursorScope()

    async def cursor_metadata(
        self, publication: CursorPublication, thread_name: str, session_id: str,
    ) -> tuple[CursorAdvancedUpdate, ...]:
        return ()

    @property
    def namespace(self) -> str:
        _reject("native namespace requires the original registry guard")

    def write(self, registry_path: Path, text: str) -> None:
        store_files._atomic_write_text(registry_path, text)

    def register_restored(self, threads: Iterable[Thread]) -> None:
        """A public bus has no coordinator identities for restored threads."""

    def launch(self, root: Path) -> RootLaunch:
        from .private_nk_entrypoint import SharedRootLaunch

        return SharedRootLaunch(root)

    def prepare_native(self, registry_path: Path) -> PrivateRegistryGuard:
        guard = PrivateRegistryGuard(registry_path, Id128Text.mint())
        guard.create_pending()
        guard.commit_initial()
        return guard

    def require_root(self, root_id: str, error: Exception) -> None:
        raise error


class PrivateRegistryGuard(DurableFile, WireWakeFile, RegistryPrivacy):
    filename = ".registry-owner-guard"

    def coordinator_schema(self) -> type[TableSchema]:
        from .private_runtime_schema import PrivateCoordinatorSchema, PrivateCoordinatorTable

        PrivateCoordinatorTable.declarations()
        return PrivateCoordinatorSchema

    def prepare_runtime(self) -> None:
        from .private_runtime_schema import PrivateCoordinatorTable

        PrivateCoordinatorTable.declarations()
        with Coordination(str(CoordinationStore.path_in(self.registry_path.parent))) as store:
            PrivateCoordinatorTable.install(store)

    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        NativeRuntimeTable.require_installed(connection)
        return NativeRuntimeInput.notification_context_sql()

    def missing_notification_assignments(
        self, sources: Sequence[Delivered],
    ) -> NotificationAssignmentRead:
        from .coordination_errors import PublicationActivationBlocked

        raise PublicationActivationBlocked("Private notification coordinator is missing")

    def __init__(self, registry_path: Path, wire_root_id: str):
        try:
            Id128Text.decode(wire_root_id)
        except MalformedRecord:
            _reject("root ID is invalid")
        self.registry_path = registry_path
        self.path = self.path_in(registry_path.parent)
        self.root_id = bytes.fromhex(wire_root_id)

    @classmethod
    def load(cls, registry_path: Path) -> PrivateRegistryGuard:
        """Read this guard's namespace; both slots and the registry must corroborate it."""
        descriptor = os.open(cls.path_in(registry_path.parent), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            raw = os.pread(descriptor, _SIZE, 0)
        finally:
            os.close(descriptor)
        if len(raw) != _SIZE:
            _reject("slot has an invalid length")
        _, _, root_id, _, _, _ = _HEADER.unpack(raw[:_HEADER.size])
        _decode(raw, root_id)
        guard = cls(registry_path, root_id.hex())
        guard.verify()
        return guard

    @property
    def namespace(self) -> str:
        """The existing native protocol namespace, never the SQLite bus identity."""
        return self.root_id.hex()

    def cursor_scope(
        self, snapshot: RegistrySnapshot, thread_name: str, session_id: str,
    ) -> CursorScope:
        from .acp_extension import LiveCursorScope, NoCursorScope
        from .native_input_owner import RegistryOwner
        from .thread_identity import ClaimedIdentity

        try:
            owner = RegistryOwner.capture_local(snapshot, thread_name)
        except RelationViolationError:
            return NoCursorScope()
        return LiveCursorScope(
            session_id, self.namespace,
            ClaimedIdentity(owner.thread.incarnation, owner.claimed_generation),
            owner.thread.require_process(),
        )

    async def cursor_metadata(
        self, publication: CursorPublication, thread_name: str, session_id: str,
    ) -> tuple[CursorAdvancedUpdate, ...]:
        return await publication.observed_metadata(thread_name, session_id)

    def launch(self, root: Path) -> RootLaunch:
        from .private_nk_entrypoint import PrivateNkLaunch

        return PrivateNkLaunch.installed(root, self.namespace)

    def prepare_native(self, registry_path: Path) -> PrivateRegistryGuard:
        if registry_path != self.registry_path:
            _reject("belongs to another registry")
        self.verify()
        return self

    def require_root(self, root_id: str, error: Exception) -> None:
        if root_id != self.namespace:
            raise error

    def _trusted_root(self) -> None:
        if os.name != "posix" or not all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "pread", "pwrite")
        ):
            _reject("requires POSIX directory and fixed-slot durability")
        if ".." in self.registry_path.parts:
            _reject("has an ambiguous root path")
        root = self.registry_path.parent.absolute()
        cursor = root
        while True:
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                _reject("has a missing directory ancestor")
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not sticky_root)
            ):
                _reject("has an untrusted directory ancestor")
            if cursor == root and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                _reject("root must be owner-only")
            if cursor == cursor.parent:
                return
            cursor = cursor.parent

    def _open(self, *, writable: bool = True) -> int:
        self._trusted_root()
        try:
            flags = os.O_RDWR if writable else os.O_RDONLY
            fd = os.open(self.path, flags | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            _reject(f"file is missing: {error.__class__.__name__}")
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size != 2 * _SIZE
        ):
            os.close(fd)
            _reject("file is not a fixed owner-only regular file")
        return fd

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
        fd = self._open(writable=False)
        try:
            seq, digest, _ = self._authority(fd)
            return seq, digest
        finally:
            os.close(fd)

    def prepare(self, new_digest: bytes) -> tuple[int, int]:
        fd = self._open()
        try:
            seq, _, index = self._authority(fd)
            if seq >= _MAX_SEQ:
                _reject("sequence is exhausted")
            self._write(fd, index, _record(self.root_id, seq + 1, _PENDING, new_digest))
            return seq + 1, index
        finally:
            os.close(fd)

    def commit(self, seq: int, index: int, new_digest: bytes) -> None:
        fd = self._open()
        try:
            current = self._slots(fd)[index]
            if current != (seq, _PENDING, new_digest):
                _reject("pending commitment changed")
            if registry_digest(self.registry_path) != new_digest:
                _reject("replacement differs from pending digest")
            self._write(fd, index, _record(self.root_id, seq, _COMMITTED, new_digest))
        finally:
            os.close(fd)

    def write(self, registry_path: Path, text: str) -> None:
        # A pending guard after any failed write MUST remain fail-closed.
        digest = hashlib.sha256(b"present\0" + text.encode("utf-8")).digest()
        sequence, slot = self.prepare(digest)
        store_files._atomic_write_text(registry_path, text)
        self.commit(sequence, slot, digest)

    def register_restored(self, threads: Iterable[Thread]) -> None:
        """Stopped subscribers still belong to frozen audiences; their coordinator identities
        must exist before any cohort can be accepted."""
        with Coordination(str(CoordinationStore.path_in(self.registry_path.parent))) as store:
            for thread in threads:
                store.participants.register(
                    thread.incarnation.require_recorded().lookup, thread.name, thread.name,
                    committed=True)

    def create_pending(self) -> None:
        """The registry lock covers explicit native preparation before coordinator setup."""
        self._trusted_root()
        if self.path.exists() or self.path.is_symlink():
            _reject("already exists before initialization")
        digest = registry_digest(self.registry_path)
        if digest != _EMPTY:
            registry_fd = os.open(self.registry_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(registry_fd)
            finally:
                os.close(registry_fd)
        platform = Platform.current()
        platform.sync_directory(self.registry_path.parent)  # baseline registry name and existing root
        fd = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if (
                os.pwrite(
                    fd,
                    _record(self.root_id, 0, _COMMITTED, digest) * 2,
                    0,
                )
                != 2 * _SIZE
            ):
                _reject("initial slots were incomplete")
            os.fsync(fd)
            platform.sync_directory(self.registry_path.parent)
            self._write(fd, 1, _record(self.root_id, 1, _PENDING, digest))
        finally:
            os.close(fd)

    def commit_initial(self) -> None:
        fd = self._open()
        try:
            first, second = self._slots(fd)
            if first != (0, _COMMITTED, registry_digest(self.registry_path)) or second != (
                1,
                _PENDING,
                first[2],
            ):
                _reject("initial pending record is invalid")
            self._write(fd, 1, _record(self.root_id, 1, _COMMITTED, first[2]))
        finally:
            os.close(fd)


@dataclass(frozen=True, kw_only=True)
class PrivateOwnerRenameIntent(DurableFile):
    """Written before a private owner rename changes either store; while it exists, private
    publication and selected execution stay blocked."""

    filename = ".private-owner-rename.pending"

    lookup: str
    old: str
    new: str
    generation: int
    wire_root_id: Annotated[str, WireName("wireRootId")]
    version: int = 1
    completed: bool = False

    def write(self, root: Path) -> None:
        store_files._atomic_write_text(self.path_in(root),
                           json.dumps(FieldCodec.encode(self), sort_keys=True))

    @classmethod
    def complete(cls, root: Path) -> None:
        """A completed record proves a finished transition if the unlink is uncertain."""
        intent = cls.path_in(root)
        replace(FieldCodec.decode(cls, json.loads(intent.read_text())), completed=True).write(root)
        intent.unlink()
        Platform.current().sync_directory(root)


def _require_no_private_owner_rename(root: Path) -> None:
    """A crashed cross-store rename cannot publish or run a selected input."""
    intent = PrivateOwnerRenameIntent.path_in(root)
    if intent.exists() or intent.is_symlink():
        raise RelationViolationError("Private owner rename is pending; inspect both authorities.")
