"""Durable, default-off wire-root maintenance phase.

A one-time upgrade from code lacking this gate must drain those original workers
before it can rely on the phase. Subsequent cutovers set Draining before
fleet drain and clear it only after known-safe publication or abort. The enabled
witness is never removed: a lost/corrupt state denies instead of returning OFF.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, MutableMapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Literal, Self

from .child_process import ProcessIdentity
from .private_path import PrivateFileRole
from .declared_family import DeclaredFamily
from .errors import RelationViolationError, ToolRefusedError
from .field_codec import (
    FieldCodec,
    ClaimId,
    MalformedRecord,
    NullMember,
    RecordMember,
    SubclassUnion,
    WireOmitDefault,
)
from .launch_runtime import LaunchRuntime
from .lifecycle import LifecycleState
from .locked_store import DurableFile, LockedStore, RegistryFile, WrittenFile
from .send_outcome import GrantedSend, HeldSend
from .store_files import (
    AtomicFile,
    HolderNamingContention,
    SharedLock,
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

    @staticmethod
    def selected[T](held: T, granted: T) -> T:
        return held


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

    @staticmethod
    def selected[T](held: T, granted: T) -> T:
        return granted


class MaintenanceTarget(SubclassUnion):
    """A hold without a target grants no additional runtime attachment."""

    @abstractmethod
    def allows_runtime(self, runtime: LaunchRuntime) -> bool: ...

    @abstractmethod
    def require_drained_writes_open_to(self, state: MaintenanceState,
                                       holder: MaintenanceHolder) -> None:
        """Whether a writer the outgoing owners settle while they drain may still commit."""


@dataclass(frozen=True)
class NoMaintenanceTarget(NullMember, MaintenanceTarget):
    def allows_runtime(self, runtime: LaunchRuntime) -> bool:
        return False

    def require_drained_writes_open_to(self, state: MaintenanceState,
                                       holder: MaintenanceHolder) -> None:
        """Draining owners still settle their own turns."""


@dataclass(frozen=True)
class TargetRuntime(LaunchRuntime, RecordMember, MaintenanceTarget):
    def allows_runtime(self, runtime: LaunchRuntime) -> bool:
        return self.prefix == runtime.prefix

    def require_drained_writes_open_to(self, state: MaintenanceState,
                                       holder: MaintenanceHolder) -> None:
        """Retargeted so the target stops the outgoing owners: from then on only the
        install's holder writes, the target's stop settling their turns among them."""
        state.require_open_to(holder)


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
    def require_held_by(cls, state: MaintenanceState, holder: MaintenanceHolder,
                       change: str) -> None:
        if not holder.holds(state.nonce):
            raise MaintenanceNotHeldError(
                f"{change} needs this root's maintenance hold; another process holds it")

    @classmethod
    def send_new(cls, delegate, *args):
        return cls.new_sends.open(delegate, *args)

    @classmethod
    def send_running(cls, delegate, *args):
        return cls.running_sends.open(delegate, *args)


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
    def require_held_by(cls, state: MaintenanceState, holder: MaintenanceHolder,
                       change: str) -> None:
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


class DeadHolderReleasedPhase(ReadyPhase):
    """The stored operator is the dead incarnation whose hold this record released."""


# The holding operation hands its nonce to its own child processes through this variable.
HOLDER_NONCE = "AGENT_COMMS_MAINTENANCE_HOLDER"


class MaintenanceHolder(SubclassUnion):
    """A process holds a ClaimId or holds nothing; only the environment has a wire spelling."""

    @abstractmethod
    def holds(self, nonce: ClaimId) -> bool: ...

    @abstractmethod
    def exported(self, environment: MutableMapping[str, str]) -> None: ...

    def request_text(self) -> str:
        """The holder a probe request carries; older workers decode empty as no holder."""
        return ""

    @classmethod
    def current(cls) -> MaintenanceHolder:
        """Decode the process's maintenance authority at its environment boundary.

        An empty value is the old-runtime spelling of no holder (interpreter_operation.py:326).
        """
        text = os.environ.get(HOLDER_NONCE)
        if not text:
            return NoMaintenanceHolder()
        return MaintenanceClaimHolder(FieldCodec.decode(ClaimId, text))

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
        self.exported(os.environ)
        try:
            yield self
        finally:
            previous.exported(os.environ)


@dataclass(frozen=True)
class NoMaintenanceHolder(NullMember, MaintenanceHolder):
    def holds(self, nonce: ClaimId) -> bool:
        return False

    def exported(self, environment: MutableMapping[str, str]) -> None:
        environment.pop(HOLDER_NONCE, None)


@dataclass(frozen=True)
class MaintenanceClaimHolder(RecordMember, MaintenanceHolder):
    nonce: ClaimId

    def holds(self, nonce: ClaimId) -> bool:
        return nonce == self.nonce

    def exported(self, environment: MutableMapping[str, str]) -> None:
        environment[HOLDER_NONCE] = self.request_text()

    def request_text(self) -> str:
        return str(self.nonce)


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

    def selected[T](self, *, held: T, granted: T) -> T:
        """The caller's answer for this phase's ingress; the barrier names no caller's facts."""
        return self.phase.new_sends.selected(held, granted)

    def require_open_to(self, holder: MaintenanceHolder) -> None:
        """Open, or held by ``holder``: the operation holding maintenance still runs its own reads."""
        self.require_open()

    def require_drained_writes_open_to(self, holder: MaintenanceHolder) -> None:
        self.require_open_to(holder)

    def require_held_by(self, holder: MaintenanceHolder, change: str) -> None:
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
    nonce: ClaimId
    phase: type[MaintenancePhase]
    target: Annotated[MaintenanceTarget, WireOmitDefault()] = field(
        default_factory=NoMaintenanceTarget
    )

    def allows_runtime(self, runtime: LaunchRuntime) -> bool:
        return self.target.allows_runtime(runtime)

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
        with MaintenanceClaimHolder(self.nonce).acting():
            yield self

    def successor(self, phase: type[MaintenancePhase], nonce: ClaimId) -> MaintenanceState:
        if not self.phase().may_become(phase()):
            raise ValueError('Maintenance transition requires a valid expected successor')
        return self.in_phase(phase, nonce)

    def in_phase(self, phase: type[MaintenancePhase], nonce: ClaimId) -> MaintenanceState:
        """Ordinary and dead-holder transitions share the phase's target lifetime."""
        return self.advanced(nonce, phase=phase, target=phase.target_after(self.target))

    def advanced(self, nonce: ClaimId, **changes: object) -> MaintenanceState:
        """Every rewrite of a hold takes the next generation and a fresh nonce."""
        return replace(self, generation=self.generation + 1, nonce=nonce, **changes)

    def require_open_to(self, holder: MaintenanceHolder) -> None:
        if not holder.holds(self.nonce):
            self.require_open()

    def require_drained_writes_open_to(self, holder: MaintenanceHolder) -> None:
        self.target.require_drained_writes_open_to(self, holder)

    def require_held_by(self, holder: MaintenanceHolder, change: str) -> None:
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
    nonce: ClaimId
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
            descriptor = PrivateFileRole.read(
                path, RelationViolationError("Maintenance witness is not private and regular"))
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RelationViolationError("Maintenance witness is inaccessible") from error
        try:
            info = os.fstat(descriptor)
            if info.st_size > 4096:
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
        self._write_unlocked(expected.in_phase(DeadHolderReleasedPhase, ClaimId.mint()))
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
        hold.require_open_to(MaintenanceHolder.current())
        return hold

    def assert_drained_writes_open_unlocked(self) -> None:
        """A carried file its draining owners settle refuses every other writer once retargeted."""
        self.current_unlocked().require_drained_writes_open_to(MaintenanceHolder.current())

    def read(self) -> MaintenanceStatus:
        return self.current_unlocked()

    def held_by(self, operator: MaintenanceOperator) -> MaintenanceState:
        """``operator``'s hold as the root records it now, in whatever phase it is in."""
        state = self.read()
        return state.phase.hold_for(self, state, operator)

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
        """Hold new turns before fleet drain; an uncertain write stays closed. A wait past
        5 s names the registry's holders in the install's log."""
        with _store_lock(self.registry_path, contention=HolderNamingContention(5)):
            current = self._current_unlocked()
            # A successor takes an abandoned hold without briefly reopening
            # ingress or making the owner repeat its install command.
            if not current.phase.holder_dead(current):
                current.require_open()
            hold = MaintenanceState(version=2, root=str(self.registry_path.parent.resolve()),
                                    generation=current.generation + 1, operator=operator,
                                    nonce=ClaimId.mint(), phase=DrainingPhase)
            self._write_unlocked(hold)
            return hold

    def retarget(self, expected: MaintenanceState, runtime: LaunchRuntime) -> MaintenanceState:
        """Transfer a drained hold to the runtime that stops its outgoing readers."""
        with _store_lock(self.registry_path):
            self.require_draining_unlocked(expected)
            hold = expected.advanced(ClaimId.mint(), target=TargetRuntime(runtime.interpreter))
            self._write_unlocked(hold)
            return hold

    def _advance_unlocked(self, expected: MaintenanceState,
                          phase: type[MaintenancePhase]) -> MaintenanceState:
        if self.current_unlocked() != expected:
            raise RelationViolationError('Maintenance generation/operator changed')
        hold = expected.successor(phase, ClaimId.mint())
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
    def allow_input(self) -> Iterator[MaintenanceStatus]:
        """Check input permission under its registry authority, never around a Bus write."""
        self.current_unlocked()
        with _store_lock(self.registry_path, mode=SharedLock()):
            status = self._current_unlocked()
            status.require_open_to(MaintenanceHolder.current())
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


class CarriedFile(DurableFile, WrittenFile):
    """A file every installation carries; its kind declares which writers maintenance refuses,
    so the install's carry check never charges another writer's change."""

    __slots__ = ()

    @classmethod
    @abstractmethod
    def require_maintenance_open_at(cls, path: Path) -> None:
        """Refuse the writers this kind's maintenance policy holds out of ``path``."""

    @classmethod
    def maintenance_at(cls, path: Path) -> MaintenanceBarrier:
        return MaintenanceBarrier(RegistryFile.path_in(cls.bus_root(path)))

    def require_maintenance_open(self) -> None:
        self.require_maintenance_open_at(self.path)


class HolderWrittenFile(CarriedFile):
    """While maintenance runs, only its holder writes this file."""

    __slots__ = ()

    @classmethod
    def require_maintenance_open_at(cls, path: Path) -> None:
        cls.maintenance_at(path).assert_open_unlocked()


class DrainSettledFile(CarriedFile):
    """Stopping owners settle their turns in this file: past the drain only the install's
    holder commits."""

    __slots__ = ()

    @classmethod
    def require_maintenance_open_at(cls, path: Path) -> None:
        cls.maintenance_at(path).assert_drained_writes_open_unlocked()


class CarriedDocument[T](LockedStore[T], HolderWrittenFile, declared_name=None):
    """A JSON document every installation carries, replaced only through its own write."""

    __slots__ = ()

    def _write_unlocked(self, text: str) -> None:
        self.require_maintenance_open()
        self._replace_unlocked(text)

    def _replace_unlocked(self, text: str) -> None:
        """The document's own atomic replacement."""
        super()._write_unlocked(text)
