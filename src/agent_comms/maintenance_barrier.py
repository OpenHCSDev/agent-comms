"""Durable, default-off wire-root maintenance phase.

A one-time upgrade from code lacking this gate must drain those original workers
before it can rely on the phase. Subsequent cutovers set Draining before
fleet drain and clear it only after known-safe publication or abort. The enabled
witness is never removed: a lost/corrupt state denies instead of returning OFF.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, Id128Text, MalformedRecord, TextRepresentation
from .lifecycle import LifecycleState
from .send_outcome import GrantedSend, HeldSend
from .store_files import (
    SharedLock,
    _async_store_lock,
    _atomic_write_text,
    _store_lock,
    store_lock_path,
)


class HeldSends:
    @staticmethod
    @contextmanager
    def open(delegate, *args):
        yield HeldSend()

    @staticmethod
    def pending() -> bool: return True

    @staticmethod
    def require_open():
        raise RelationViolationError("Maintenance ingress requires READY")


class GrantedSends:
    @staticmethod
    @contextmanager
    def open(delegate, *args):
        if delegate is None:
            yield GrantedSend()
        else:
            with delegate(*args) as outcome:
                yield outcome

    @staticmethod
    def pending() -> bool: return False

    @staticmethod
    def require_open():
        pass


class MaintenancePhase(DeclaredFamily, LifecycleState, affix="Phase"):
    new_sends = running_sends = HeldSends

    @classmethod
    def send_new(cls, delegate, *args):
        return cls.new_sends.open(delegate, *args)

    @classmethod
    def send_running(cls, delegate, *args):
        return cls.running_sends.open(delegate, *args)


class DrainingPhase(MaintenancePhase):
    running_sends = GrantedSends

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
    new_sends = running_sends = GrantedSends

    @classmethod
    def successors(cls):
        return (DrainingPhase,)


class MaintenanceStatus:
    """Absent witness is READY; stored phases derive from their record."""

    phase = ReadyPhase
    generation = 0

    @property
    def turn_pending(self) -> bool:
        return self.phase.new_sends.pending()

    def require_open(self) -> None:
        self.phase.new_sends.require_open()


@dataclass(frozen=True)
class MaintenanceOperator:
    value: str

    def __post_init__(self) -> None:
        if not self.value or len(self.value) > 128:
            raise ValueError('Maintenance operator must have bounded identity')


class MaintenanceOperatorText(TextRepresentation):
    @classmethod
    def from_text(cls, value: str) -> MaintenanceOperator:
        return MaintenanceOperator(value)

    @classmethod
    def encode(cls, value: MaintenanceOperator) -> str:
        return value.value


MaintenanceOperatorField = Annotated[MaintenanceOperator, MaintenanceOperatorText]


@dataclass(frozen=True, kw_only=True)
class MaintenanceMarker:
    version: Literal[1]
    root: str
    generation: int

    def __post_init__(self):
        if not 0 < self.generation < 1 << 63:
            raise ValueError("Maintenance generation is outside its allocation domain")


@dataclass(frozen=True, kw_only=True)
class MaintenanceState(MaintenanceMarker, MaintenanceStatus):
    operator: MaintenanceOperatorField
    nonce: str
    phase: type[MaintenancePhase]

    def __post_init__(self):
        super().__post_init__()
        Id128Text.decode(self.nonce)

    def require_marker(self, marker: MaintenanceMarker, root: Path) -> MaintenanceState:
        if (
            marker
            != MaintenanceMarker(
                version=self.version, root=str(root.resolve()), generation=self.generation
            )
            or self.root != marker.root
        ):
            raise RelationViolationError("Maintenance witness inconsistent; new turns held")
        return self

    def successor(self, phase: type[MaintenancePhase], nonce: str) -> MaintenanceState:
        if not self.phase().may_become(phase()):
            raise ValueError('Maintenance transition requires a valid expected successor')
        return MaintenanceState(version=self.version, root=self.root,
                                generation=self.generation + 1, operator=self.operator,
                                nonce=nonce, phase=phase)

    def require_draining(self) -> None:
        if self.phase is not DrainingPhase:
            raise ValueError('Only an acquired draining phase can be cleared')


class MaintenanceIngress(ABC):
    @abstractmethod
    def ingress_async(self) -> AbstractAsyncContextManager[MaintenanceStatus]: ...


class UncoordinatedIngress(MaintenanceIngress):
    @asynccontextmanager
    async def ingress_async(self) -> AsyncIterator[MaintenanceStatus]:
        yield MaintenanceStatus()


UNCOORDINATED_INGRESS = UncoordinatedIngress()


class MaintenanceBarrier(MaintenanceIngress):
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
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                value = FieldCodec.decode(declaration, FieldCodec.parse(stream.read()))
        except (OSError, MalformedRecord) as error:
            raise RelationViolationError("Maintenance witness is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return value

    def current_unlocked(self) -> MaintenanceStatus:
        """Call while holding the registry lock OR the wire lock.

        For a send, the wire lock stays held through stdin.write; for a claim,
        the registry lock spans the turn's durable transition. A phase change
        takes both locks in wire -> registry order.
        """
        marker = self._read(self.marker_path, MaintenanceMarker)
        state = self._read(self.state_path, MaintenanceState)
        if marker is None and state is None:
            return MaintenanceStatus()  # Never-enabled roots have the same READY phase.
        if marker is None or state is None:
            raise RelationViolationError("Maintenance witness incomplete; new turns held")
        return state.require_marker(marker, self.registry_path.parent)

    def assert_open_unlocked(self) -> MaintenanceStatus:
        hold = self.current_unlocked()
        hold.require_open()
        return hold

    def read(self) -> MaintenanceStatus:
        with _store_lock(self.wire_path, mode=SharedLock()):
            return self.current_unlocked()

    def _write_unlocked(self, hold: MaintenanceState) -> None:
        marker = MaintenanceMarker(version=hold.version, root=hold.root,
                                   generation=hold.generation)
        hold.require_marker(marker, self.registry_path.parent)
        marker_text = json.dumps(FieldCodec.encode(marker), sort_keys=True)
        state_text = json.dumps(FieldCodec.encode(hold), sort_keys=True)
        # The marker leads so an interrupted replacement still holds new turns.
        _atomic_write_text(self.marker_path, marker_text, fsync_parent=True)
        _atomic_write_text(self.state_path, state_text, fsync_parent=True)

    def begin(self, operator: MaintenanceOperator) -> MaintenanceState:
        """Hold new turns before fleet drain; an uncertain write stays closed."""
        with _store_lock(self.wire_path), _store_lock(self.registry_path):
            current = self.current_unlocked()
            if current.phase is not ReadyPhase:
                raise RelationViolationError('Maintenance already active or uncertain')
            hold = MaintenanceState(version=1, root=str(self.registry_path.parent.resolve()),
                                    generation=current.generation + 1, operator=operator,
                                    nonce=Id128Text.mint(), phase=DrainingPhase)
            self._write_unlocked(hold)
            return hold

    def _advance_unlocked(self, expected: MaintenanceState,
                          phase: type[MaintenancePhase]) -> MaintenanceState:
        if self.current_unlocked() != expected:
            raise RelationViolationError('Maintenance generation/operator changed')
        hold = expected.successor(phase, Id128Text.mint())
        self._write_unlocked(hold)
        return hold

    def advance(self, expected: MaintenanceState,
                phase: type[MaintenancePhase]) -> MaintenanceState:
        with _store_lock(self.wire_path), _store_lock(self.registry_path):
            return self._advance_unlocked(expected, phase)

    def clear(self, expected: MaintenanceState) -> MaintenanceState:
        """Finish a known-safe drain or abort; never infer safety from an error."""
        expected.require_draining()
        return self.advance(expected, ReadyPhase)

    def clear_stopped(self, expected: MaintenanceState, wire_descriptor: int) -> MaintenanceState:
        """Clear while the stopped batch holds its original exclusive wire OFD.

        Reacquiring the wire by name would deadlock. An unrelated descriptor
        cannot publish READY while another cutover holds the stopped workers' wire lock.
        """
        expected.require_draining()
        if type(wire_descriptor) is not int or wire_descriptor < 0:
            raise RelationViolationError('Maintenance needs the stopped wire descriptor')
        opened = os.fstat(wire_descriptor)
        named = store_lock_path(self.wire_path).stat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise RelationViolationError('Maintenance wire lock changed')
        fcntl.flock(wire_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _store_lock(self.registry_path):
            return self._advance_unlocked(expected, ReadyPhase)

    def require_draining_unlocked(self, expected: MaintenanceState) -> None:
        """Require this cutover's captured phase while holding the wire lock."""
        expected.require_draining()
        if self.current_unlocked() != expected:
            raise RelationViolationError(
                'Cutover maintenance phase changed before owner retirement'
            )

    @contextmanager
    def allow_input(self, *, blocking: bool = True) -> Iterator[MaintenanceStatus]:
        """Hold the root lock through one synchronous input operation.

        Do not await while inside this context. A child still needs to perform
        its own claim/send checks, and old-import children are not protected.
        Maintenance, stop and rename take the exclusive wire lock. Independent
        inputs share this gate; their registry, input and journal owners retain
        their own determining locks.
        """
        with _store_lock(self.wire_path, mode=SharedLock(), blocking=blocking):
            yield self.assert_open_unlocked()

    @asynccontextmanager
    async def ingress_async(self) -> AsyncIterator[MaintenanceStatus]:
        """Acquire the same ingress gate without blocking a native event loop.

        The borrower writes synchronously, then releases before waiting for
        pipe drain or a native response.
        """
        async with _async_store_lock(self.wire_path, mode=SharedLock()):
            yield self.current_unlocked()
