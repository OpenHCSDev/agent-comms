"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import agent_events as events
from .activity import ObservedActivity
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import NullMember, RecordMember, SubclassUnion, boundary_decoder
from .input_disposition import InputDispositions
from .locked_store import JsonDocument
from .message_reference import MessageReference
from .mro_dispatch import MroDispatch, handles
from .routing import TurnRouting
from .store_files import _store_lock
from .thread_identity import ThreadIncarnation, TurnFence, TurnId, TurnIdentity
from .turn_phase import CancellingPhase, IdlePhase, PreparingPhase, TurnPhase

if TYPE_CHECKING:
    from .child_process import ProcessState
    from .messaging import Messaging
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .turn_runner import TurnRunner


class ReportTurn(SubclassUnion, affix="ReportTurn"):
    @abstractmethod
    def matches_current(self, turn: TurnDeclaration) -> bool: ...


@dataclass(frozen=True)
class IdleReportTurn(NullMember, ReportTurn):
    def matches_current(self, turn: TurnDeclaration) -> bool:
        return False


@dataclass(frozen=True)
class RunningReportTurn(RecordMember, ReportTurn):
    turn_id: str
    generation: int

    def __post_init__(self) -> None:
        if not self.turn_id or type(self.generation) is not int or self.generation <= 0:
            raise ValueError("A reporting turn requires a recorded ID and generation")

    def matches_current(self, turn: TurnDeclaration) -> bool:
        return turn.matches_report(self.turn_id, self.generation)


class CompactionObservation(MroDispatch):
    """The turn declaration owns compaction transitions, not its runner."""
    def __init__(
        self, runner: TurnRunner, session_id: str, lease: TurnLeaseFence, phase: TurnPhase,
    ) -> None:
        self.runner, self.session_id, self.lease, self.phase = runner, session_id, lease, phase

    async def publish(self, phase: TurnPhase) -> None:
        await self.runner.transition_turn(self.session_id, self.lease, phase)

    @handles(events.CompactionStart)
    async def start(self, event: events.CompactionStart) -> None:
        await self.publish(self.phase.compacting())

    @handles(events.CompactionEnd)
    async def end(self, event: events.CompactionEnd) -> None:
        await self.publish(self.phase.compaction_ended())


class TurnEnding(DeclaredFamily, affix="Ending"):
    """Decode the context-manager exit at its boundary, not at every consumer."""

    @staticmethod
    def from_exception(error: BaseException) -> FailedEnding:
        return FailedEnding("".join(traceback.format_exception(error)))

    @staticmethod
    @boundary_decoder
    def from_context(error: BaseException | None) -> TurnEnding:
        """Decode contextlib's external exception-or-None protocol once."""
        return FINISHED_ENDING if error is None else TurnEnding.from_exception(error)

    @abstractmethod
    def record(self, turn: ActiveTurn) -> EndedTurn: ...


class FinishedEnding(TurnEnding):
    def record(self, turn: ActiveTurn) -> EndedTurn:
        return EndedTurn(turn)


FINISHED_ENDING = FinishedEnding()


@dataclass(frozen=True)
class FailedEnding(TurnEnding):
    error: str

    def record(self, turn: ActiveTurn) -> FailedTurn:
        return FailedTurn(turn, self.error)


class TurnDeclaration(SubclassUnion, affix=""):
    """A recorded active turn or the idle state."""

    phase: TurnPhase
    started_at: float | None

    def project_activity(self, activity: ObservedActivity) -> ObservedActivity:
        return self.phase.project_activity(activity)

    phase = IdlePhase()
    started_at = None
    executing = False

    @property
    def report_turn(self) -> str:
        return ""

    def require_active(self) -> ActiveTurn:
        raise RelationViolationError("registry owner has no active turn")

    def has_claim(self, claim_scope: int) -> bool:
        return False

    def validate_owner(self, process: ProcessState, generation: int) -> None:
        pass

    def require_idle(self) -> None:
        pass

    def require_current(self, claim_scope: int, generation: int) -> None:
        pass

    def require_turn(self, turn_id: TurnId) -> None:
        raise RelationViolationError("live owner does not hold the requested turn")

    def without_claim(self) -> TurnDeclaration:
        return self

    def observed_id(self, finished: str | None) -> str | None:
        return finished

    def started_by(self, updated_at_ms: int) -> bool:
        return False

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]:
        return iter(())

    def matches(self, turn_id: TurnId) -> bool:
        return False

    def held_by(self, lease: TurnLeaseFence) -> bool:
        return False

    def observed_task_turn(self, identity: TurnIdentity, turn: TurnId, thread: Thread) -> bool:
        return (
            identity.generation == thread.turn_generation
            and thread.last_finished_turn_id == turn.value
        )

    def selection_matches(self, actual: object, expected: object) -> bool:
        return actual == expected

    def answered_by(self, messaging: Messaging, owner: Thread,
                    target: str) -> tuple[MessageReference, ...]:
        return ()

    async def observe_compaction(self, runner: TurnRunner, session_id: str, owner: Thread,
                                 event: events.CompactionEvent) -> None:
        pass

    async def cancel_turn(self, runner: TurnRunner, session_id: str, owner: Thread) -> None:
        pass

    def report_wait_turn(self) -> ReportTurn:
        return IdleReportTurn()

    def matches_report(self, turn_id: str, generation: int) -> bool:
        return False

    def ended(self, lease: TurnLeaseFence, ending: TurnEnding) -> IdleTurn:
        return NoTurn()

    def release_witnesses(self, lease: TurnLeaseFence) -> Iterator[ActiveTurn]:
        return iter(())


class IdleTurn(TurnDeclaration, declared_name=None):
    """Only ended states can publish their terminal record and consequences."""

    def write(self, record: StoredTurnRecord) -> None:
        pass

    def notify(self, messaging: Messaging, name: str) -> None:
        pass


@dataclass(frozen=True)
class NoTurn(NullMember, IdleTurn):
    id = None


@dataclass(frozen=True)
class EndedTurn(RecordMember, IdleTurn):
    """An exact ended turn retains evidence, never executable authority."""

    turn: ActiveTurn

    @property
    def id(self) -> str:
        return self.turn.id

    def release_witnesses(self, lease: TurnLeaseFence) -> Iterator[ActiveTurn]:
        return self.turn.release_witnesses(lease)

    def write(self, record: StoredTurnRecord) -> None:
        record._publish_unlocked(self)


@dataclass(frozen=True)
class FailedTurn(EndedTurn):
    error: str

    def notify(self, messaging: Messaging, name: str) -> None:
        messaging.report_failed_turn(name, self)


@dataclass(frozen=True)
class ActiveTurn(RecordMember, TurnDeclaration):
    id: str
    owner_pid: int
    started_at: float = field(default_factory=time.time)
    routing: TurnRouting | None = None
    claimed_generation: int | None = field(
        default=None, metadata={"wire_name": "admission_generation"}
    )
    turn_generation: int | None = None
    phase: TurnPhase = field(default_factory=PreparingPhase)
    executing = True

    @property
    def report_turn(self) -> str:
        return self.id

    def selection_matches(self, actual: object, expected: object) -> bool:
        return True

    def answered_by(self, messaging: Messaging, owner: Thread,
                    target: str) -> tuple[MessageReference, ...]:
        lease = owner.require_turn_lease()
        inputs = InputDispositions(messaging.root).in_turn(self.id)
        with messaging.bus.log.checked_read() as source:
            return inputs.answered_by(lease, source, messaging.registry.snapshot(), target)

    async def observe_compaction(self, runner: TurnRunner, session_id: str, owner: Thread,
                                 event: events.CompactionEvent) -> None:
        await CompactionObservation(
            runner, session_id, owner.require_turn_lease(), self.phase
        ).dispatch(event)

    async def cancel_turn(self, runner: TurnRunner, session_id: str, owner: Thread) -> None:
        await runner.transition_turn(session_id, owner.require_turn_lease(), CancellingPhase())

    def report_wait_turn(self) -> ReportTurn:
        if self.claimed_generation is None or self.turn_generation is None:
            return IdleReportTurn()
        return RunningReportTurn(self.id, self.turn_generation)

    def matches_report(self, turn_id: str, generation: int) -> bool:
        return self.claimed_generation is not None and (self.id, self.turn_generation) == (
            turn_id,
            generation,
        )

    def require_active(self) -> ActiveTurn:
        return self

    def has_claim(self, claim_scope: int) -> bool:
        return self.claimed_generation == claim_scope

    def validate_owner(self, process: ProcessState, generation: int) -> None:
        if not process.owns_turn(self):
            raise RelationViolationError("A turn must belong to the registered executor.")
        if self.turn_generation is not None and (
            type(self.turn_generation) is not int
            or self.turn_generation <= 0 or self.turn_generation != generation
        ):
            raise RelationViolationError("Active turn generation differs from its owner.")

    def require_idle(self) -> None:
        raise RelationViolationError("live owner already has an active turn")

    def require_current(self, claim_scope: int, generation: int) -> None:
        if not self.current(claim_scope, generation):
            raise RelationViolationError("live owner turn claim_scope is no longer current")

    def require_turn(self, turn_id: TurnId) -> None:
        if not self.matches(turn_id):
            raise RelationViolationError("live owner does not hold the requested turn")

    def matches(self, turn_id: TurnId) -> bool:
        return TurnId(self.id) == turn_id

    def without_claim(self) -> ActiveTurn:
        return replace(self, claimed_generation=None)

    def ended(self, lease: TurnLeaseFence, ending: TurnEnding) -> IdleTurn:
        return ending.record(self) if self.may_release(lease) else NoTurn()

    def release_witnesses(self, lease: TurnLeaseFence) -> Iterator[ActiveTurn]:
        if self.may_release(lease):
            yield self

    def observed_task_turn(self, identity: TurnIdentity, turn: TurnId, thread: Thread) -> bool:
        self.validate_owner(thread.process_identity, thread.turn_generation)
        return self.claimed_generation is not None and self.turn_generation is not None and (
            self.matches_report(turn.value, identity.generation)
            or identity.generation == self.turn_generation - 1
        )

    def held_by(self, lease: TurnLeaseFence) -> bool:
        return self.id == lease.turn_id and self.current(
            lease.claimed_generation, lease.identity.generation)

    def may_release(self, lease: TurnLeaseFence) -> bool:
        return self.held_by(lease) or (
            self.claimed_generation is None
            and replace(self, claimed_generation=lease.claimed_generation).held_by(lease))

    def fence(self, incarnation: ThreadIncarnation, record: StoredTurnRecord) -> TurnLeaseFence:
        if self.claimed_generation is None or self.turn_generation is None:
            raise RelationViolationError("An authorized original turn is required")
        return TurnLeaseFence(TurnIdentity(incarnation, self.turn_generation), self.id,
                              self.claimed_generation, record=record)

    def observed_id(self, finished: str | None) -> str:
        return self.id

    def started_by(self, updated_at_ms: int) -> bool:
        return self.started_at * 1000 <= updated_at_ms + 1

    def project_activity(self, activity: ObservedActivity) -> ObservedActivity:
        return replace(super().project_activity(activity),
                       timestamp=max(activity.timestamp, self.started_at))

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]:
        if phase != self.phase:
            yield replace(self, phase=phase)

    def owned_by(self, pid: int, claimed_generation: int) -> bool:
        """Check that this turn's local process and registered input generation agree."""
        return (
            self.owner_pid == pid
            and self.claimed_generation == claimed_generation
            and claimed_generation > 0
        )

    def current(self, claimed_generation: int, turn_generation: int) -> bool:
        """Compare persisted turn witnesses with the current registry authority."""
        return (
            self.claimed_generation == claimed_generation
            and self.turn_generation == turn_generation
            and turn_generation > 0
        )


@dataclass(frozen=True)
class TurnState:
    """Read-only capture of the existing lease, not another state store."""

    active: TurnDeclaration = field(default_factory=NoTurn)
    finished_turn_id: str | None = None

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnState]:
        for active in self.active.phase_effects(phase):
            yield replace(self, active=active)

    def native_phase_effects(self, phase: TurnPhase) -> Iterator[TurnState]:
        yield from self.phase_effects(self.active.phase.observed(phase))

    def matches(self, turn_id: str | None) -> bool:
        return turn_id is None or self.active.observed_id(self.finished_turn_id) == turn_id


@dataclass(frozen=True, slots=True, eq=False)
class TurnLeaseFence(TurnFence):
    """Exact local begin-turn lease and its original record."""

    record: StoredTurnRecord = field(kw_only=True)


class TurnRecord(SubclassUnion, affix=""):
    """A stored turn reference or the declared absence of one; never a phase cache."""

    @abstractmethod
    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> TurnRecord: ...

    @abstractmethod
    def read(self) -> TurnDeclaration: ...

    @abstractmethod
    def without_claim(self) -> None: ...

    def require_lease(
        self,
        incarnation: ThreadIncarnation,
        process: ProcessState,
        generation: int,
    ) -> TurnLeaseFence:
        self.read().require_active()
        raise RelationViolationError("An authorized original turn is required")


@dataclass(frozen=True)
class NoTurnRecord(NullMember, TurnRecord):
    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> NoTurnRecord:
        return self

    def read(self) -> NoTurn:
        return NoTurn()

    def without_claim(self) -> None:
        pass


@dataclass(frozen=True)
class StoredTurnReference(RecordMember, TurnRecord):
    """Wire presence only; the registry's thread supplies the turn identity."""

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:
        return StoredTurnRecord.for_identity(registry_path, identity)

    def read(self) -> TurnDeclaration:
        raise RelationViolationError("Turn reference must be bound by its registry")

    def without_claim(self) -> None:
        self.read()


@dataclass(frozen=True)
class StoredTurnRecord(JsonDocument[TurnDeclaration], StoredTurnReference, declared_name=None):
    """Turn file bound to the registry that decoded its wire reference."""

    identity: TurnIdentity = field(metadata={"wire_transient": True})
    registry_path: Path = field(repr=False, compare=False, metadata={"wire_transient": True})

    record_type = TurnDeclaration

    @property
    def path(self) -> Path:  # type: ignore[override]  # Derived from its registry.
        identity = self.identity
        name = f"{identity.incarnation.require_recorded().lookup}-{identity.generation}.json"
        return self.registry_path.parent / "turns" / name

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:
        record = super().bound_to(registry_path, identity)
        return (self if (self.registry_path, self.identity) ==
                (record.registry_path, record.identity) else record)

    def empty(self) -> TurnDeclaration:
        raise RelationViolationError(f"Active turn record is missing: {self.path}")

    @classmethod
    def for_identity(cls, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:
        return cls(identity, registry_path.resolve())

    @classmethod
    def begin(
        cls, registry_path: Path, identity: TurnIdentity, turn: ActiveTurn,
    ) -> StoredTurnRecord:
        record = cls.for_identity(registry_path, identity)
        record.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record._publish_unlocked(turn)
        return record

    def require_lease(
        self, incarnation: ThreadIncarnation, process: ProcessState, generation: int,
    ) -> TurnLeaseFence:
        active = self.read().require_active()
        active.validate_owner(process, generation)
        return active.fence(incarnation, self)

    def without_claim(self) -> None:
        self._publish_unlocked(self.read().without_claim())

    def end(
        self,
        lease: TurnLeaseFence,
        ending: TurnEnding = FINISHED_ENDING,
    ) -> IdleTurn:
        """End the exact original turn without depending on a readable registry."""
        with _store_lock(self.registry_path):
            ended = self.read().ended(lease, ending)
            ended.write(self)
        return ended

    def _turn_effects(
        self, lease: TurnLeaseFence, observe: Callable[[TurnState], Iterable[TurnState]],
    ) -> tuple[TurnState, ...]:
        with _store_lock(self.registry_path):
            active = self.read()
            if not active.held_by(lease):
                return ()
            effects = tuple(observe(TurnState(active)))
            for state in effects:
                self._publish_unlocked(state.active.require_active())
            return effects

    def transition(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        return self._turn_effects(lease, lambda state: state.phase_effects(phase))

    def observe_native_phase(
        self, lease: TurnLeaseFence, phase: TurnPhase,
    ) -> tuple[TurnState, ...]:
        return self._turn_effects(lease, lambda state: state.native_phase_effects(phase))


class TurnCompletion(ABC):
    """Whether terminal publication attested an exact finished turn."""

    @abstractmethod
    def idle_sources(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]: ...


@dataclass(frozen=True, slots=True)
class NoFinishedTurn(TurnCompletion):
    def idle_sources(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        return ()


class FinishedTurnFence(TurnFence, TurnCompletion):
    """Durable exact-turn completion witness, not a reply or model grant."""

    def idle_sources(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:
        return snapshot.idle_after(self)
