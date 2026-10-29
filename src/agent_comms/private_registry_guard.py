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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .child_process import Platform
from . import store_files
from .coordination_database import CoordinationStore
from .cohort_schema import AcceptanceFrontier
from .coordination_errors import PublicationActivationBlocked
from .coordination_schema import CoordinatorTable
from .coordination_tables.participants import Participants
from .coordinator import Coordination
from .errors import RelationViolationError
from .field_codec import RootId, ThreadLookup
from .locked_store import RollbackRetained, WireWakeFile
from .native_input_owner import RegistryOwner
from .native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from .private_nk_entrypoint import (
    PrivateNkLaunch,
    SharedRootLaunch,
)
from .private_path import PrivateFileRole
from .private_runtime_schema import PrivateCoordinatorSchema, PrivateCoordinatorTable
from .thread_identity import ClaimedIdentity
from .notification_evidence import NotificationAssignmentRead, NotificationSelection


if TYPE_CHECKING:
    from .acp_extension import CursorAdvancedUpdate, CursorScope
    from .bus_publication import Delivered
    from .cursor_publication import CursorPublication
    from .presentation import OutcomeViewer
    from .private_nk_entrypoint import RootLaunch
    from .registry_document import RegistrySnapshot
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
        fd = PrivateFileRole.read(
            path, RelationViolationError("Private registry guard registry is not a bounded owner-only regular file"))
    except FileNotFoundError:
        return _EMPTY
    try:
        info = os.fstat(fd)
        if info.st_size > _MAX_REGISTRY:
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
    def namespace(self) -> RootId:
        """Require the original native namespace; a public registry has none."""

    @abstractmethod
    def write(self, registry_path: Path, text: str) -> None: ...

    @abstractmethod
    def launch(self, root: Path) -> RootLaunch: ...

    @abstractmethod
    def prepare_native(self, registry_path: Path) -> PrivateRegistryGuard: ...

    @abstractmethod
    def require_root(self, root_id: RootId, error: Exception) -> None: ...

    @abstractmethod
    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        """Context evidence for every row in the captured assignment window ``w``."""

    @abstractmethod
    def context_entered_sql(self, assignment: str) -> str:
        """Whether a native input committed ``assignment``'s message to its session context."""

    @abstractmethod
    def missing_notification_assignments(
        self, selection: NotificationSelection,
