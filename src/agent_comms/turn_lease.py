"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .activity import ObservedActivity
from .channel_targets import CommandTarget
from .child_process import ProcessIdentity, ProcessState
from .coordination_session import NativeBoundary
from .errors import RelationViolationError
from .field_codec import (
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
    TurnId,
    WireTransient,
    boundary_decoder,
)
from .goal_presentation import RunningExecution
from .input_disposition import InputDispositions
from .lifecycle_capability import Executing, LifecycleCapability
from .locked_store import DurableFile, JsonDocument, PatternFile
from .message_reference import MessageReference
from .read_basis import ChannelConversation, DMConversation
from .reported_turn import NoReportedTurn, ReportedTurn, ReportedTurnState
from .routing import NoTurnRouting, Routing
from .store_files import SharedLock, _store_lock
from .thread_identity import (
    NoObservedTurn,
    ObservedTurn,
    ThreadIncarnation,
    TurnFence,
    TurnIdentity,
)
from .turn_phase import IdlePhase, PreparingPhase, TurnPhase

if TYPE_CHECKING:
    from .goal_presentation import GoalExecution, GoalExecutionSource
    from .messaging import Messaging
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class TurnDeclaration(LifecycleCapability, SubclassUnion, affix=""):
    """A recorded active turn or the idle state."""

    phase: TurnPhase
    id: TurnId | None
    started_at: float | None

    @abstractmethod
    def execution(self, source: GoalExecutionSource) -> GoalExecution: ...

    def project_activity(self, activity: ObservedActivity) -> ObservedActivity:
        return self.phase.project_activity(activity)

    @property
    @abstractmethod
    def reported_turn(self) -> ReportedTurnState: ...

    @abstractmethod
    def require_active(self) -> ActiveTurn: ...

    @abstractmethod
    def has_claim(self, claim_scope: int) -> bool: ...

    @abstractmethod
    def validate_owner(self, process: ProcessState, generation: int) -> None: ...

    @abstractmethod
    def require_idle(self) -> None: ...

    @abstractmethod
    def require_current(self, claim_scope: int, generation: int) -> None: ...

    @abstractmethod
    def require_turn(self, turn_id: TurnId) -> None: ...

    @abstractmethod
    def without_claim(self) -> TurnDeclaration: ...

    @abstractmethod
    def observed_turn(
        self, finished: TurnRecord, identity: TurnIdentity, claim_scope: int,
    ) -> ObservedTurn: ...

    @abstractmethod
    def started_by(self, updated_at_ms: int) -> bool: ...

    @abstractmethod
    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]: ...

    @abstractmethod
    def matches(self, turn_id: TurnId) -> bool: ...

    @abstractmethod
    def settled_by(self, state: TurnState) -> bool:
        """Whether an idle publication settles this held turn."""


@dataclass(frozen=True)
class NoTurn(NullMember, TurnDeclaration):
    phase: ClassVar[TurnPhase] = IdlePhase()
    id = None
    started_at = None
    reported_turn: ClassVar[ReportedTurnState] = NoReportedTurn()

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        return source.generation.lifecycle.idle_execution(source)

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

    def without_claim(self) -> NoTurn:
        return self

    def observed_turn(
        self, finished: TurnRecord, identity: TurnIdentity, claim_scope: int,
    ) -> ObservedTurn:
        return finished.observed_turn(identity, claim_scope)

    def started_by(self, updated_at_ms: int) -> bool:
        return False

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]:
        return iter(())

    def matches(self, turn_id: TurnId) -> bool:
        return False

    def settled_by(self, state: TurnState) -> bool:
        return True


class TurnGeneration(SubclassUnion, affix="Generation"):
    """A registry generation the turn file recorded, or none."""

    @abstractmethod
    def matches(self, generation: int) -> bool: ...

    @abstractmethod
    def owns_generation(self, generation: int) -> bool:
        """The owner at this generation may hold the turn."""

    @abstractmethod
    def adopted_by(self, claimed: int) -> TurnGeneration:
        """The claim generation a lease releasing this turn may hold it under."""

    @abstractmethod
    def fence(
        self, claimed: TurnGeneration, incarnation: ThreadIncarnation, turn_id: TurnId,
        record: StoredTurnRecord,
    ) -> TurnLease: ...

    @abstractmethod
    def claim_fence(
        self, identity: TurnIdentity, turn_id: TurnId, record: StoredTurnRecord,
    ) -> TurnLease: ...


@dataclass(frozen=True, slots=True)
class RecordedGeneration(SingleFieldValue, TurnGeneration):
    value: int

    def matches(self, generation: int) -> bool:
        return self.value == generation

    def owns_generation(self, generation: int) -> bool:
        return self.value > 0 and self.value == generation

    def adopted_by(self, claimed: int) -> RecordedGeneration:
        return self

    def fence(
        self, claimed: TurnGeneration, incarnation: ThreadIncarnation, turn_id: TurnId,
        record: StoredTurnRecord,
    ) -> TurnLease:
        return claimed.claim_fence(TurnIdentity(incarnation, self.value), turn_id, record)

    def claim_fence(
        self, identity: TurnIdentity, turn_id: TurnId, record: StoredTurnRecord,
    ) -> TurnLeaseFence:
        return TurnLeaseFence(identity, turn_id, self.value, record=record)


@dataclass(frozen=True, slots=True)
class UnrecordedGeneration(NullMember, TurnGeneration):
    """The turn file carries no registry generation: never leased, or its claim was released."""

    def matches(self, generation: int) -> bool:
        return False

    def owns_generation(self, generation: int) -> bool:
        return True

    def adopted_by(self, claimed: int) -> RecordedGeneration:
        return RecordedGeneration(claimed)

    def fence(
        self, claimed: TurnGeneration, incarnation: ThreadIncarnation, turn_id: TurnId,
        record: StoredTurnRecord,
    ) -> NoTurnLease:
        return NoTurnLease()

    def claim_fence(
        self, identity: TurnIdentity, turn_id: TurnId, record: StoredTurnRecord,
    ) -> NoTurnLease:
        return NoTurnLease()


@dataclass(frozen=True)
class ActiveTurn(Executing, RecordMember, TurnDeclaration):
    id: TurnId
    owner: ProcessIdentity
    started_at: float = field(default_factory=time.time)
    routing: Routing = NoTurnRouting()
    claimed_generation: TurnGeneration = UnrecordedGeneration()
    turn_generation: TurnGeneration = UnrecordedGeneration()
    phase: TurnPhase = field(default_factory=PreparingPhase)
    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        if source.owner_alive and source.owner.process_identity.owns_turn(self):
            return RunningExecution(source.goal.id)
        return source.generation.lifecycle.idle_execution(source)

    @property
    def reported_turn(self) -> ReportedTurnState:
        return ReportedTurn(self.id)

    def require_active(self) -> ActiveTurn:
        return self

    def has_claim(self, claim_scope: int) -> bool:
        return self.claimed_generation.matches(claim_scope)

    def validate_owner(self, process: ProcessState, generation: int) -> None:
        if not process.owns_turn(self):
            raise RelationViolationError("A turn must belong to the registered executor.")
        if not self.turn_generation.owns_generation(generation):
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
        return self.id == turn_id

    def settled_by(self, state: TurnState) -> bool:
        return state.active.matches(self.id) or state.finished.names(self.id)

    def without_claim(self) -> ActiveTurn:
        return replace(self, claimed_generation=UnrecordedGeneration())

    def held_by(self, lease: TurnLeaseFence) -> bool:
        return self.id == lease.turn_id and self.current(
            lease.claimed_generation, lease.identity.generation)

    def may_release(self, lease: TurnLeaseFence) -> bool:
        adopted = self.claimed_generation.adopted_by(lease.claimed_generation)
        return replace(self, claimed_generation=adopted).held_by(lease)

    def lease(
        self, incarnation: ThreadIncarnation, record: StoredTurnRecord,
    ) -> TurnLease:
        return self.turn_generation.fence(self.claimed_generation, incarnation, self.id, record)

    def observed_turn(
        self, finished: TurnRecord, identity: TurnIdentity, claim_scope: int,
    ) -> TurnFence:
        return TurnFence(identity, self.id, claim_scope)

    def started_by(self, updated_at_ms: int) -> bool:
        return self.started_at * 1000 <= updated_at_ms + 1

    def project_activity(self, activity: ObservedActivity) -> ObservedActivity:
        return replace(super().project_activity(activity),
                       timestamp=max(activity.timestamp, self.started_at))

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]:
        if phase != self.phase:
            yield replace(self, phase=phase)

    def owned_by(self, process: ProcessState, claimed_generation: int) -> bool:
        """Check that this turn's local process and registered input generation agree."""
        return (
            process.owns_turn(self)
            and self.claimed_generation.matches(claimed_generation)
            and claimed_generation > 0
        )

    def current(self, claimed_generation: int, turn_generation: int) -> bool:
        """Compare persisted turn witnesses with the current registry authority."""
        return (
            self.claimed_generation.matches(claimed_generation)
            and self.turn_generation.matches(turn_generation)
            and turn_generation > 0
        )


class TurnLease(ABC):
    """A thread's exact local turn lease, or the declared absence of one."""

    leased: ClassVar[bool]

    @abstractmethod
    def require(self, refusal: Exception) -> TurnLeaseFence: ...

    @abstractmethod
    def held(self) -> tuple[TurnLeaseFence, ...]: ...

    @abstractmethod
    def answered_inputs(
        self, messaging: Messaging, owner: Thread, target: str,
    ) -> tuple[MessageReference, ...]:
        """What a message from this owner to target answers."""

    @abstractmethod
    def attests(self, claim_scope: int, turn_id: TurnId) -> bool:
        """This lease holds ``turn_id`` under ``claim_scope``."""

    @abstractmethod
    def authored(self, identity: TurnIdentity, turn: TurnId) -> bool: ...

    @abstractmethod
    def observes_task_turn(self, identity: TurnIdentity, turn: TurnId, idle: bool) -> bool:
        """During a turn, its exact ID or the preceding generation; idle, the caller's answer."""

    @abstractmethod
    @boundary_decoder
    def reports(self, turn_id: TurnId | None, generation: int | None) -> bool:
        """A stored wait's reporting turn is this lease's turn."""

    @abstractmethod
    @boundary_decoder
    def stored_turn_id(self) -> TurnId | None:
        """The reporting turn ID a stored goal wait records."""

    @abstractmethod
    @boundary_decoder
    def stored_generation(self) -> int | None:
        """The turn generation a stored goal wait records."""


@dataclass(frozen=True)
class NoTurnLease(NullMember, TurnLease):
    """The thread holds no local turn."""

    leased = False

    def require(self, refusal: Exception) -> TurnLeaseFence:
        raise refusal

    def held(self) -> tuple[TurnLeaseFence, ...]:
        return ()

    def answered_inputs(
        self, messaging: Messaging, owner: Thread, target: str,
    ) -> tuple[MessageReference, ...]:
        with NativeBoundary(messaging.bus) as boundary, messaging.bus.bus.reading() as source:
            snapshot = boundary.registry
            route = CommandTarget.named(target)
            conversation = route.conversation_key(
                owner.name, snapshot, ChannelConversation, DMConversation,
            )
            reads = messaging.bus.reads
            document = reads.read_conversation(owner.name, conversation, snapshot)
            cursor = reads.cursor(owner.name, snapshot, document=document)
            references = source.delivered_unanswered(
                owner.incarnation.require_recorded(), conversation.scope(snapshot),
                cursor, snapshot,
            )
            destination = route.canonical_name(snapshot.canonical_name)
            return tuple(
                original.message.reference
                for original in source.messages_for_references(references)
                if original.message.exact_reply_target == destination
            )

    def attests(self, claim_scope: int, turn_id: TurnId) -> bool:
        return False

    def authored(self, identity: TurnIdentity, turn: TurnId) -> bool:
        return False

    def observes_task_turn(self, identity: TurnIdentity, turn: TurnId, idle: bool) -> bool:
        return idle

    @boundary_decoder
    def reports(self, turn_id: TurnId | None, generation: int | None) -> bool:
        return False

    @boundary_decoder
    def stored_turn_id(self) -> TurnId | None:
        return None

    @boundary_decoder
    def stored_generation(self) -> int | None:
        return None


@dataclass(frozen=True, slots=True, eq=False)
class TurnLeaseFence(TurnFence, TurnLease):
    """Exact local begin-turn lease and its original record."""

    record: StoredTurnRecord = field(kw_only=True)
    leased = True

    def require(self, refusal: Exception) -> TurnLeaseFence:
        return self

    def held(self) -> tuple[TurnLeaseFence, ...]:
        return (self,)

    def answered_inputs(
        self, messaging: Messaging, owner: Thread, target: str,
    ) -> tuple[MessageReference, ...]:
        inputs = InputDispositions(messaging.root).in_turn(self.turn_id)
        with NativeBoundary(messaging.bus) as boundary, messaging.bus.bus.reading() as source:
            return inputs.answered_by(
                self, source, boundary.registry, target, privacy=boundary.privacy,
            )

    def attests(self, claim_scope: int, turn_id: TurnId) -> bool:
        return self.can_attest(claim_scope) and self.turn_id == turn_id

    def authored(self, identity: TurnIdentity, turn: TurnId) -> bool:
        return (self.identity, self.turn_id) == (identity, turn)

    def observes_task_turn(self, identity: TurnIdentity, turn: TurnId, idle: bool) -> bool:
        return self.authored(identity, turn) or identity.generation == self.identity.generation - 1

    @boundary_decoder
    def reports(self, turn_id: TurnId | None, generation: int | None) -> bool:
        return (turn_id, generation) == (self.turn_id, self.identity.generation)

    @boundary_decoder
    def stored_turn_id(self) -> TurnId | None:
        return self.turn_id

    @boundary_decoder
    def stored_generation(self) -> int | None:
        return self.identity.generation


class TurnRecord(LifecycleCapability, SubclassUnion, affix=""):
    """A stored turn reference or the declared absence of one; never a phase cache."""

    @abstractmethod
    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> TurnRecord: ...

    @abstractmethod
    def read(self) -> TurnDeclaration: ...

    @abstractmethod
    def without_claim(self) -> None: ...

    @abstractmethod
    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> TurnLease: ...

    def state(self) -> TurnState:
        return TurnState(self.read())

    def released(self) -> TurnRecord:
        """What stays once the live reference is dropped."""
        return self

    def require_generation(self, generation: int) -> None:
        pass

    def names(self, turn_id: TurnId) -> bool:
        return False


    @abstractmethod
    def observed_turn(self, identity: TurnIdentity, claim_scope: int) -> ObservedTurn: ...

@dataclass(frozen=True)
class NoTurnRecord(NullMember, TurnRecord):

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> NoTurnRecord:
        return self

    def read(self) -> NoTurn:
        return NoTurn()

    def without_claim(self) -> None:
        pass

    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> NoTurnLease:
        return NoTurnLease()


    def observed_turn(self, identity: TurnIdentity, claim_scope: int) -> NoObservedTurn:
        return NoObservedTurn()

@dataclass(frozen=True)
class FinishedTurn(RecordMember, TurnRecord):
    """No live turn; the last one finished with this ID."""

    turn_id: TurnId

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> FinishedTurn:
        return self

    def read(self) -> NoTurn:
        return NoTurn()

    def state(self) -> TurnState:
        return TurnState(finished=self)

    def names(self, turn_id: TurnId) -> bool:
        return self.turn_id == turn_id

    def without_claim(self) -> None:
        pass

    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> NoTurnLease:
        return NoTurnLease()

    def require_generation(self, generation: int) -> None:
        if generation == 0:
            raise ValueError("Finished turn requires a prior turn generation.")


    def observed_turn(self, identity: TurnIdentity, claim_scope: int) -> TurnFence:
        return TurnFence(identity, self.turn_id, claim_scope)

@dataclass(frozen=True)
class StoredTurnReference(RecordMember, TurnRecord):
    """Wire presence only; the registry's thread supplies the turn identity."""

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:
        return StoredTurnRecord.for_identity(registry_path, identity)

    def read(self) -> TurnDeclaration:
        raise RelationViolationError("Turn reference must be bound by its registry")

    def without_claim(self) -> None:
        self.read()

    def released(self) -> NoTurnRecord:
        return NoTurnRecord()

    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> TurnLease:
        raise RelationViolationError("Turn reference must be bound by its registry")


    def observed_turn(self, identity: TurnIdentity, claim_scope: int) -> TurnFence:
        raise RelationViolationError("Turn reference must be bound by its registry")

@dataclass(frozen=True)
class StoredTurnRecord(Executing, DurableFile, PatternFile, JsonDocument[ActiveTurn],
                       StoredTurnReference, declared_name=None):
    """Turn file bound to the registry that decoded its wire reference."""

    identity: Annotated[TurnIdentity, WireTransient()]
    registry_path: Annotated[Path, WireTransient()] = field(
        repr=False, compare=False
    )

    record_type = ActiveTurn
    filename = "turns/{lookup}-{generation}.json"

    @property
    def path(self) -> Path:  # type: ignore[override]  # Derived from its registry.
        identity = self.identity
        return self.path_in(self.registry_path.parent,
                            lookup=identity.incarnation.require_recorded().lookup,
                            generation=str(identity.generation))

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:
        record = super().bound_to(registry_path, identity)
        return (self if (self.registry_path, self.identity) ==
                (record.registry_path, record.identity) else record)

    def empty(self) -> ActiveTurn:
        raise RelationViolationError(f"Active turn record is missing: {self.path}")

    def read(self) -> TurnDeclaration:  # type: ignore[override]  # A successor may be finished.
        """This turn while its thread still names it, else the thread's current turn.

        RegistryEdit.commit unlinks a superseded file under the registry lock, so the
        registry read under that same lock settles which generation is current
        (RegistrySnapshot.current_turn); a caller's older snapshot never names a deleted file.
        """
        from .registry_store import RegistryStore

        with _store_lock(self.registry_path, mode=SharedLock()):
            current = RegistryStore(self.registry_path).read_snapshot_unlocked().current_turn(self)
            return self._read_unlocked() if current == self else current.read()

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

    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> TurnLease:
        active = self._read_unlocked()
        active.validate_owner(process, generation)
        return active.lease(incarnation, self)

    def without_claim(self) -> None:
        self._publish_unlocked(self._read_unlocked().without_claim())

    def _turn_effects(
        self, lease: TurnLeaseFence, observe: Callable[[TurnState], Iterable[TurnState]],
    ) -> tuple[TurnState, ...]:
        from .registry_store import RegistryStore

        with _store_lock(self.registry_path):
            if not RegistryStore(self.registry_path).read_snapshot_unlocked().holds_turn(self.identity):
                return ()
            active = self._read_unlocked()
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


@dataclass(frozen=True)
class TurnState:
    """Read-only capture of the existing lease, not another state store."""

    active: TurnDeclaration = field(default_factory=NoTurn)
    finished: TurnRecord = field(default_factory=NoTurnRecord)

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnState]:
        for active in self.active.phase_effects(phase):
            yield replace(self, active=active)

    def native_phase_effects(self, phase: TurnPhase) -> Iterator[TurnState]:
        yield from self.phase_effects(self.active.phase.observed(phase))

    def settles(self, held: TurnDeclaration) -> bool:
        """A busy publication always replaces the held turn; an idle one only settles its own."""
        return self.active.phase.busy or held.settled_by(self)


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
