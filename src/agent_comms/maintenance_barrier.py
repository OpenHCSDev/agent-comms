"""Durable, default-off wire-root maintenance phase.

A one-time upgrade from code lacking this gate must drain those original workers
before it can rely on the phase. Subsequent cutovers set Draining before
fleet drain and clear it only after known-safe publication or abort. The enabled
witness is never removed: a lost/corrupt state denies instead of returning OFF.
"""

from __future__ import annotations

import json
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Literal, Self

from .child_process import Platform, ProcessIdentity
from .declared_family import DeclaredFamily
from .errors import RelationViolationError, ToolRefusedError
from .field_codec import (
    FieldCodec,
    Id128Text,
    MalformedRecord,
    NullMember,
    RecordMember,
    SubclassUnion,
    WireOmitDefault,
)
from .launch_runtime import LaunchRuntime
from .lifecycle import LifecycleState
from .locked_store import DurableFile, LockedStore, RegistryFile
from .send_outcome import GrantedSend, HeldSend
from .store_files import (
    AtomicFile,
    SharedLock,
    StoreLockContention,
    _async_store_lock,
    _store_lock,
)


class MaintenanceHeldError(RelationViolationError, ToolRefusedError):
    """The bus is in maintenance: its readers close and wait for READY."""


class MaintenanceNotHeldError(RelationViolationError):
    """Work only this root's maintenance holder may do, attempted by a process that does not
    hold it."""

    @classmethod
    def unheld(cls, change: str) -> MaintenanceNotHeldError:
        return cls(f"{change} needs this root's maintenance hold, and no install holds it: "
                   "only agent-comms install changes it")


class HeldSends:
    @staticmethod
    @contextmanager
    def open(delegate, *args):
        yield HeldSend()

    @staticmethod
    def pending() -> bool: return True

    @staticmethod
    def require_open():
        raise MaintenanceHeldError("Maintenance holds ingress: the bus is under maintenance")


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


class MaintenanceTarget(SubclassUnion):
    """A hold without a target grants no additional runtime attachment."""

    @abstractmethod
    def allows_runtime(self, runtime: LaunchRuntime) -> bool: ...


@dataclass(frozen=True)
class NoMaintenanceTarget(NullMember, MaintenanceTarget):
    def allows_runtime(self, runtime: LaunchRuntime) -> bool:
        return False


@dataclass(frozen=True)
class TargetRuntime(LaunchRuntime, RecordMember, MaintenanceTarget):
    def allows_runtime(self, runtime: LaunchRuntime) -> bool:
        return self.prefix == runtime.prefix


class MaintenancePhase(DeclaredFamily, LifecycleState, affix="Phase"):
    new_sends = running_sends = HeldSends

    @classmethod
    def target_after(cls, target: MaintenanceTarget) -> MaintenanceTarget:
        return target

    @classmethod
    def holder_dead(cls, state: MaintenanceState) -> bool:
        return not state.operator.alive()

    @classmethod
    def hold_for(cls, barrier: MaintenanceBarrier, state: MaintenanceStatus,
                 operator: MaintenanceOperator) -> MaintenanceState:
        raise MaintenanceHeldError('Another maintenance operation holds this root')

    @classmethod
    def require_held_by(cls, state: MaintenanceState, holder: str, change: str) -> None:
        if holder != state.nonce:
            raise MaintenanceNotHeldError(
                f"{change} needs this root's maintenance hold; another process holds it")

    @classmethod
    def send_new(cls, delegate, *args):
        return cls.new_sends.open(delegate, *args)

    @classmethod
    def send_running(cls, delegate, *args):
        return cls.running_sends.open(delegate, *args)

    @classmethod
    def hold_restore(cls, barrier: MaintenanceBarrier, operator: MaintenanceOperator,
                     drained: Callable[[], None]) -> None:
        """A failed upgrade's restore runs under the hold it began, still in place here."""


class DrainingPhase(MaintenancePhase):
    running_sends = GrantedSends

    @classmethod
    def hold_for(cls, barrier: MaintenanceBarrier, state: MaintenanceState,
                 operator: MaintenanceOperator) -> MaintenanceState:
        if state.operator != operator:
            raise MaintenanceHeldError('Another maintenance operation holds this root')
        return state

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
    def require_held_by(cls, state: MaintenanceState, holder: str, change: str) -> None:
        raise MaintenanceNotHeldError.unheld(change)

    @classmethod
    def target_after(cls, target: MaintenanceTarget) -> MaintenanceTarget:
        return NoMaintenanceTarget()

    @classmethod
    def holder_dead(cls, state: MaintenanceState) -> bool:
        return False

    @classmethod
    def hold_for(cls, barrier: MaintenanceBarrier, state: MaintenanceStatus,
                 operator: MaintenanceOperator) -> MaintenanceState:
        return barrier.begin(operator)

    @classmethod
    def successors(cls):
        return (DrainingPhase,)

    @classmethod
    def hold_restore(cls, barrier: MaintenanceBarrier, operator: MaintenanceOperator,
                     drained: Callable[[], None]) -> None:
        """The round trip failed on the reopened bus: hold it again and wait for its readers
        to drain; the restored bus brings back the upgrade's own hold."""
        barrier.begin(operator)
        drained()


class DeadHolderReleasedPhase(ReadyPhase):
    """The stored operator is the dead incarnation whose hold this record released."""


# The holding operation hands its nonce to its own child processes through this variable.
HOLDER_NONCE = "AGENT_COMMS_MAINTENANCE_HOLDER"


@dataclass(frozen=True)
class MaintenanceHolder:
    """The hold a process acts under; an empty nonce holds nothing. It travels in
    ``HOLDER_NONCE`` to every child the process starts."""

    nonce: str

    @classmethod
    def current(cls) -> MaintenanceHolder:
        return cls(os.environ.get(HOLDER_NONCE, ""))

    @staticmethod
    def outside(environment: Mapping[str, str]) -> dict[str, str]:
        """``environment`` as a child that outlives holds starts with it: it takes each
        request's holder from the request, never from its launch."""
        child = dict(environment)
        child.pop(HOLDER_NONCE, None)
        return child

    @contextmanager
    def acting(self) -> Iterator[Self]:
        """This process, and every child it starts, acts as this holder while the block
        lasts, restoring its previous holder afterward."""
        previous = MaintenanceHolder.current()
        os.environ[HOLDER_NONCE] = self.nonce
        try:
            yield self
        finally:
            os.environ[HOLDER_NONCE] = previous.nonce


class MaintenanceStatus:
    """Absent witness is READY; stored phases derive from their record."""

    phase = ReadyPhase
    generation = 0

    @property
    def turn_pending(self) -> bool:
        return self.phase.new_sends.pending()

    def allows_runtime(self, runtime: LaunchRuntime) -> bool:
        return False

    def observed(self, barrier: MaintenanceBarrier) -> MaintenanceStatus:
        return self

    async def observed_async(self, barrier: MaintenanceBarrier) -> MaintenanceStatus:
        return self

    def require_open(self) -> None:
        self.phase.new_sends.require_open()

    def require_open_to(self, holder: str) -> None:
        """Open, or held by ``holder``: the operation holding maintenance still runs its own reads."""
        self.require_open()

    def require_held_by(self, holder: str, change: str) -> None:
        """``change`` runs only in the process holding this root's maintenance: a root that
        never enabled maintenance has no holder."""
        raise MaintenanceNotHeldError.unheld(change)


@dataclass(frozen=True, eq=False)
class MaintenanceOperator(ProcessIdentity, declared_name=None):
    """A hold belongs to this exact process, using the existing identity codec."""

    @classmethod
    def current(cls) -> Self:
        identity = ProcessIdentity.capture(os.getpid())
        return cls(identity.pid, identity.start_time)


@dataclass(frozen=True, kw_only=True)
class MaintenanceMarker(DurableFile):
    filename = ".maintenance-enabled"
    version: Literal[2]
    root: str
    generation: int

    def __post_init__(self):
        if not 0 < self.generation < 1 << 63:
            raise ValueError("Maintenance generation is outside its allocation domain")


@dataclass(frozen=True, kw_only=True)
class MaintenanceState(MaintenanceMarker, MaintenanceStatus):
    filename = ".maintenance-state"
    operator: MaintenanceOperator
    nonce: str
    phase: type[MaintenancePhase]
    target: Annotated[MaintenanceTarget, WireOmitDefault()] = field(
        default_factory=NoMaintenanceTarget
    )

    def allows_runtime(self, runtime: LaunchRuntime) -> bool:
        return self.target.allows_runtime(runtime)

    def __post_init__(self):
        super().__post_init__()
        Id128Text.decode(self.nonce)

    def observed(self, barrier: MaintenanceBarrier) -> MaintenanceStatus:
        if self.phase.holder_dead(self):
            return barrier.release_dead(self)
        return self

    async def observed_async(self, barrier: MaintenanceBarrier) -> MaintenanceStatus:
        if self.phase.holder_dead(self):
            return await barrier.release_dead_async(self)
        return self

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

    @contextmanager
    def holding(self) -> Iterator[Self]:
        """This process, and every child it starts, acts as this hold's holder while the
        block lasts."""
        with MaintenanceHolder(self.nonce).acting():
            yield self

    def successor(self, phase: type[MaintenancePhase], nonce: str) -> MaintenanceState:
        if not self.phase().may_become(phase()):
            raise ValueError('Maintenance transition requires a valid expected successor')
        return self.in_phase(phase, nonce)

    def in_phase(self, phase: type[MaintenancePhase], nonce: str) -> MaintenanceState:
        """Ordinary and dead-holder transitions share the phase's target lifetime."""
        return self.advanced(nonce, phase=phase, target=phase.target_after(self.target))

    def advanced(self, nonce: str, **changes: object) -> MaintenanceState:
        """Every rewrite of a hold takes the next generation and a fresh nonce."""
        return replace(self, generation=self.generation + 1, nonce=nonce, **changes)

    def require_open_to(self, holder: str) -> None:
        if holder != self.nonce:
            self.require_open()

    def require_held_by(self, holder: str, change: str) -> None:
        self.phase.require_held_by(self, holder, change)

    def require_draining(self) -> None:
        if self.phase is not DrainingPhase:
            raise ValueError('Only an acquired draining phase can be cleared')


@dataclass(frozen=True, kw_only=True)
class HoldTarget(DurableFile):
    """The runtime a hold admits, kept beside the witness. Only runtimes that know targets
    read this file, so the witness keeps the shape every runtime that may still observe,
    restore or recover this root reads. It applies only to the witness write that stamped it:
    an interrupted write leaves no target, the closed side."""

    filename = ".maintenance-target"
    generation: int
    nonce: str
    target: MaintenanceTarget

    def applied_to(self, state: MaintenanceState) -> MaintenanceState:
        if (self.generation, self.nonce) != (state.generation, state.nonce):
            return state
        return replace(state, target=self.target)


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

    Phase transitions and input permission use the registry's existing lock.
    This is cooperative same-UID maintenance control, not authority over old code
    or hostile clients.
    """

    def __init__(self, registry_path: Path):
        self.registry_path = Path(registry_path)
        self.marker_path = MaintenanceMarker.path_in(self.registry_path.parent)
        self.state_path = MaintenanceState.path_in(self.registry_path.parent)
        self.target_path = HoldTarget.path_in(self.registry_path.parent)

    @property
    def witness_paths(self) -> tuple[Path, ...]:
        return self.marker_path, self.state_path, self.target_path

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
                or not Platform.current().is_private(info, 0o600)
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

    def _current_unlocked(self) -> MaintenanceStatus:
        """Read the paired witness; incomplete publication always refuses.

        Admission reads under the registry lock that phase transitions own.
        An unlocked observation can refuse work, never authorize a mutation.
        """
        marker = self._read(self.marker_path, MaintenanceMarker)
        state = self._read(self.state_path, MaintenanceState)
        if marker is None and state is None:
            return MaintenanceStatus()  # Never-enabled roots have the same READY phase.
        if marker is None or state is None:
            raise RelationViolationError("Maintenance witness incomplete; new turns held")
        state = state.require_marker(marker, self.registry_path.parent)
        target = self._read(self.target_path, HoldTarget)
        return state if target is None else target.applied_to(state)

    def current_unlocked(self) -> MaintenanceStatus:
        return self._current_unlocked().observed(self)

    def _release_dead_unlocked(self, expected: MaintenanceState) -> MaintenanceStatus:
        current = self._current_unlocked()
        if current != expected:
            return current
        self._write_unlocked(expected.in_phase(DeadHolderReleasedPhase, Id128Text.mint()))
        raise MaintenanceHeldError(
            f"Maintenance released dead holder pid={expected.operator.pid} "
            f"start_time={expected.operator.start_time}; ingress reopened"
        )

    def release_dead(self, expected: MaintenanceState) -> MaintenanceStatus:
        # Registry acquisition serializes maintenance writes. Observation releases
        # a dead holder before borrowing a shared registry input scope.
        with _store_lock(self.registry_path):
            return self._release_dead_unlocked(expected)

    async def release_dead_async(self, expected: MaintenanceState) -> MaintenanceStatus:
        async with _async_store_lock(self.registry_path):
            return self._release_dead_unlocked(expected)

    def assert_open_unlocked(self) -> MaintenanceStatus:
        hold = self.current_unlocked()
        hold.require_open_to(MaintenanceHolder.current().nonce)
        return hold

    def read(self) -> MaintenanceStatus:
        return self.current_unlocked()

    def _write_unlocked(self, hold: MaintenanceState) -> None:
        marker = MaintenanceMarker(version=hold.version, root=hold.root,
                                   generation=hold.generation)
        hold.require_marker(marker, self.registry_path.parent)
        marker_text = json.dumps(FieldCodec.encode(marker), sort_keys=True)
        # The witness never carries the target: a rollback restores the outgoing runtime,
        # which reads the witness strictly and has no target field.
        state_text = json.dumps(FieldCodec.encode(replace(hold, target=NoMaintenanceTarget())),
                                sort_keys=True)
        target_text = json.dumps(FieldCodec.encode(HoldTarget(
            generation=hold.generation, nonce=hold.nonce, target=hold.target)), sort_keys=True)
        # The target leads, stamped for this write; the marker leads the state so an
        # interrupted replacement still holds new turns.
        AtomicFile(self.target_path).write_text(target_text)
        AtomicFile(self.marker_path).write_text(marker_text)
        AtomicFile(self.state_path).write_text(state_text)

    def begin(self, operator: MaintenanceOperator) -> MaintenanceState:
        """Hold new turns before fleet drain; an uncertain write stays closed."""
        with _store_lock(self.registry_path):
            current = self._current_unlocked()
            # A successor takes an abandoned hold without briefly reopening
            # ingress or making the owner repeat its install command.
            if not current.phase.holder_dead(current):
                current.require_open()
            hold = MaintenanceState(version=2, root=str(self.registry_path.parent.resolve()),
                                    generation=current.generation + 1, operator=operator,
                                    nonce=Id128Text.mint(), phase=DrainingPhase)
            self._write_unlocked(hold)
            return hold

    def retarget(self, expected: MaintenanceState, runtime: LaunchRuntime) -> MaintenanceState:
        """Transfer a drained hold after its outgoing readers have stopped."""
        with _store_lock(self.registry_path):
            self.require_draining_unlocked(expected)
            hold = expected.advanced(Id128Text.mint(), target=TargetRuntime(runtime.interpreter))
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
        with _store_lock(self.registry_path):
            return self._advance_unlocked(expected, phase)

    def clear(self, expected: MaintenanceState) -> MaintenanceState:
        """Finish a known-safe drain or abort; never infer safety from an error."""
        expected.require_draining()
        return self.advance(expected, ReadyPhase)

    def require_draining_unlocked(self, expected: MaintenanceState) -> None:
        """Require this cutover's captured phase under the registry lock."""
        expected.require_draining()
        if self.current_unlocked() != expected:
            raise RelationViolationError(
                'Maintenance generation/operator changed'
            )

    @contextmanager
    def allow_input(self, *, blocking: bool = True) -> Iterator[MaintenanceStatus]:
        """Check input permission under its registry authority, never around a Bus write."""
        self.current_unlocked()
        with _store_lock(
            self.registry_path, mode=SharedLock(),
            contention=StoreLockContention.for_blocking(blocking),
        ):
            status = self._current_unlocked()
            status.require_open_to(MaintenanceHolder.current().nonce)
            yield status

    @asynccontextmanager
    async def ingress_async(self) -> AsyncIterator[MaintenanceStatus]:
        """Acquire the same ingress gate without blocking a native event loop.

        The borrower writes synchronously, then releases before waiting for
        pipe drain or a native response.
        """
        await self._current_unlocked().observed_async(self)
        async with _async_store_lock(self.registry_path, mode=SharedLock()):
            yield self._current_unlocked()


class CarriedDocument[T](DurableFile, LockedStore[T], declared_name=None):
    """A JSON document every installation carries: while maintenance runs, only its holder
    replaces it, so the install's carry check never charges another writer's change."""

    __slots__ = ()

    def require_maintenance_open(self) -> None:
        """Refuse every writer but the maintenance holder."""
        MaintenanceBarrier(RegistryFile.path_in(self.bus_root(self.path))).assert_open_unlocked()

    def _write_unlocked(self, text: str) -> None:
        self.require_maintenance_open()
        self._replace_unlocked(text)

    def _replace_unlocked(self, text: str) -> None:
        """The document's own atomic replacement."""
        super()._write_unlocked(text)
