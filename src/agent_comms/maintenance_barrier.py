"""Durable, default-off wire-root maintenance admission.

A one-time upgrade from code lacking this gate must drain those original workers
before it can rely on phase admission. Subsequent cutovers set Draining before
fleet drain and clear it only after known-safe publication or abort. The enabled
witness is never removed: a lost/corrupt state denies instead of returning OFF.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from uuid import uuid4
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .lifecycle import LifecycleState

from .bus_publication import unique_wire_object
from .errors import RelationViolationError
from .store_files import _async_store_lock, _atomic_write_text, _store_lock, store_lock_path


class MaintenancePhase(DeclaredFamily, LifecycleState, affix="Phase"):
    turn_pending = True

    @classmethod
    def require_open(cls) -> None:
        if cls.turn_pending:
            raise RelationViolationError("Maintenance admission closed")


class DrainingPhase(MaintenancePhase):
    @classmethod
    def successors(cls):
        # A wire-compatible cutover clears directly; format migrations may
        # proceed through Paused and Installing before they become READY.
        return (PausedPhase, ReadyPhase)


class PausedPhase(MaintenancePhase):
    @classmethod
    def successors(cls):
        return (InstallingPhase,)


class InstallingPhase(MaintenancePhase):
    @classmethod
    def successors(cls):
        return (ReadyPhase,)


class ReadyPhase(MaintenancePhase):
    turn_pending = False

    @classmethod
    def successors(cls):
        return (DrainingPhase,)


@dataclass(frozen=True)
class MaintenanceReceipt:
    generation: int
    operator: str
    nonce: str
    phase: type[MaintenancePhase]


@dataclass(frozen=True, kw_only=True)
class MaintenanceMarker:
    version: Literal[1]
    root: str
    generation: int

    def __post_init__(self):
        if not 0 < self.generation < 1 << 63:
            raise ValueError("Maintenance generation is outside its allocation domain")


@dataclass(frozen=True, kw_only=True)
class MaintenanceState(MaintenanceMarker):
    operator: str
    nonce: str
    phase: type[MaintenancePhase]

    def __post_init__(self):
        super().__post_init__()
        if not self.operator or len(self.nonce) != 32:
            raise ValueError("Maintenance requires operator and nonce")

    def require_marker(self, marker: MaintenanceMarker, root: Path):
        if (
            marker
            != MaintenanceMarker(
                version=self.version, root=str(root.resolve()), generation=self.generation
            )
            or self.root != marker.root
        ):
            raise RelationViolationError("Maintenance witness inconsistent; admission closed")
        return MaintenanceReceipt(self.generation, self.operator, self.nonce, self.phase)


class MaintenanceBarrier:
    """One root's phase owner; writer and readers use the same durable witness.

    A phase transition acquires the wire lock, then the registry lock.
    This is cooperative same-UID maintenance control, not authority over old code
    or hostile clients.
    """

    def __init__(self, registry_path: Path):
        self.registry_path = Path(registry_path)
        self.wire_path = self.registry_path.parent / "wire"
        self.marker_path = self.registry_path.parent / ".maintenance-enabled"
        self.state_path = self.registry_path.parent / ".maintenance-state"

    @property
    def witness_paths(self) -> tuple[Path, Path]:
        return self.marker_path, self.state_path

    @staticmethod
    def _read(path: Path, declaration):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RelationViolationError("Maintenance witness is inaccessible") from error
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or (os.name == "posix" and info.st_uid != os.geteuid())
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
                or info.st_nlink != 1
                or info.st_size > 4096
            ):
                raise RelationViolationError("Maintenance witness is not private and regular")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                value = FieldCodec.decode(
                    declaration, json.load(stream, object_pairs_hook=unique_wire_object)
                )
        except (OSError, UnicodeError, ValueError) as error:
            raise RelationViolationError("Maintenance witness is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return value

    def current_unlocked(self) -> MaintenanceReceipt | None:
        """Call while holding the registry lock OR the wire lock.

        For a send, the wire lock stays held through stdin.write; for a claim,
        the registry lock spans the turn's durable transition. A phase change
        takes both locks in wire -> registry order.
        """
        marker = self._read(self.marker_path, MaintenanceMarker)
        state = self._read(self.state_path, MaintenanceState)
        if marker is None and state is None:
            return None  # Only fresh, never-enabled roots are default OFF.
        if marker is None or state is None:
            raise RelationViolationError("Maintenance witness incomplete; admission closed")
        return state.require_marker(marker, self.registry_path.parent)

    def assert_open_unlocked(self) -> MaintenanceReceipt | None:
        receipt = self.current_unlocked()
        if receipt is not None:
            receipt.phase.require_open()
        return receipt

    def read(self) -> MaintenanceReceipt | None:
        with _store_lock(self.wire_path, shared=True):
            return self.current_unlocked()

    def _write_unlocked(self, receipt: MaintenanceReceipt) -> None:
        root = str(self.registry_path.parent.resolve())
        marker = MaintenanceMarker(version=1, root=root, generation=receipt.generation)
        state = MaintenanceState(version=1, root=root, generation=receipt.generation,
                                 operator=receipt.operator, nonce=receipt.nonce,
                                 phase=receipt.phase)
        # The marker leads so an interrupted replacement always denies admission.
        _atomic_write_text(self.marker_path, json.dumps(FieldCodec.encode(marker), sort_keys=True), fsync_parent=True)
        _atomic_write_text(self.state_path, json.dumps(FieldCodec.encode(state), sort_keys=True), fsync_parent=True)

    def begin(self, operator: str) -> MaintenanceReceipt:
        """Close turn admission before fleet drain; an uncertain write stays closed."""
        if type(operator) is not str or not operator or len(operator) > 128:
            raise ValueError('Maintenance operator must have bounded identity')
        with _store_lock(self.wire_path), _store_lock(self.registry_path):
            current = self.current_unlocked()
            if current is not None and current.phase is not ReadyPhase:
                raise RelationViolationError('Maintenance already active or uncertain')
            receipt = MaintenanceReceipt(
                (current.generation + 1) if current is not None else 1,
                operator, uuid4().hex, DrainingPhase,
            )
            self._write_unlocked(receipt)
            return receipt

    def _advance_unlocked(self, expected: MaintenanceReceipt,
                          phase: type[MaintenancePhase]) -> MaintenanceReceipt:
        if (type(expected) is not MaintenanceReceipt or not isinstance(phase, type)
                or not issubclass(phase, MaintenancePhase)
                or not expected.phase().may_become(phase())):
            raise ValueError('Maintenance transition requires a valid expected successor')
        if self.current_unlocked() != expected:
            raise RelationViolationError('Maintenance generation/operator changed')
        receipt = MaintenanceReceipt(expected.generation + 1, expected.operator,
                                     uuid4().hex, phase)
        self._write_unlocked(receipt)
        return receipt

    def advance(self, expected: MaintenanceReceipt,
                phase: type[MaintenancePhase]) -> MaintenanceReceipt:
        with _store_lock(self.wire_path), _store_lock(self.registry_path):
            return self._advance_unlocked(expected, phase)

    def clear(self, expected: MaintenanceReceipt) -> MaintenanceReceipt:
        """Finish a known-safe drain or abort; never infer safety from an error."""
        if type(expected) is not MaintenanceReceipt or expected.phase is not DrainingPhase:
            raise ValueError('Only an acquired draining phase can be cleared directly')
        return self.advance(expected, ReadyPhase)

    def clear_stopped(self, expected: MaintenanceReceipt, wire_descriptor: int) -> MaintenanceReceipt:
        """Clear while the stopped batch holds its original exclusive wire OFD.

        Reacquiring the wire by name would deadlock. An unrelated descriptor
        cannot publish READY while another cutover holds the stopped workers' wire lock.
        """
        if type(expected) is not MaintenanceReceipt or expected.phase is not DrainingPhase:
            raise ValueError('Only an acquired draining phase can be cleared')
        if type(wire_descriptor) is not int or wire_descriptor < 0:
            raise RelationViolationError('Maintenance needs the stopped wire descriptor')
        opened = os.fstat(wire_descriptor)
        named = store_lock_path(self.wire_path).stat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise RelationViolationError('Maintenance wire lock changed')
        fcntl.flock(wire_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _store_lock(self.registry_path):
            return self._advance_unlocked(expected, ReadyPhase)

    def require_draining_unlocked(self, expected: MaintenanceReceipt) -> None:
        """Admit only this cutover's captured phase while holding the wire lock."""
        if (type(expected) is not MaintenanceReceipt or expected.phase is not DrainingPhase
                or self.current_unlocked() != expected):
            raise RelationViolationError('Cutover maintenance phase changed before owner retirement')

    def pending_turn(self) -> MaintenanceReceipt | None:
        """Return the cutover-owned typed reason to retain, not launch, a wake.

        Missing/corrupt witnesses still fail closed rather than looking like READY.
        """
        receipt = self.read()
        return receipt if receipt is not None and receipt.phase.turn_pending else None

    @contextmanager
    def admit_ingress(self, *, blocking: bool = True) -> Iterator[MaintenanceReceipt | None]:
        """Share open-root custody through one synchronous ingress operation.

        Do not await while inside this context. A child still needs to perform
        its own claim/send checks, and old-import children are not protected.
        Maintenance, stop and rename take exclusive wire custody. Independent
        inputs share this gate; their registry, input and journal owners retain
        their own determining locks.
        """
        with _store_lock(self.wire_path, shared=True, blocking=blocking):
            yield self.assert_open_unlocked()

    @asynccontextmanager
    async def admit_ingress_async(self):
        """Acquire the same ingress gate without blocking a native event loop.

        The borrower writes synchronously, then releases before waiting for
        pipe drain or a native response.
        """
        async with _async_store_lock(self.wire_path, shared=True):
            yield self.assert_open_unlocked()
