"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .activity import ObservedActivity
from .errors import RelationViolationError
from .field_codec import NullMember, RecordMember, SubclassUnion
from .locked_store import JsonDocument
from .routing import TurnRouting
from .store_files import _store_lock
from .thread_identity import ThreadIncarnation, TurnFence, TurnId, TurnIdentity
from .turn_phase import IdlePhase, PreparingPhase, TurnPhase

if TYPE_CHECKING:
    from .child_process import ProcessState


class TurnDeclaration(SubclassUnion):
    """A recorded active turn or the idle state."""

    phase: TurnPhase
    id: str | None
    started_at: float | None

    def project_activity(self, activity: ObservedActivity) -> ObservedActivity:
        return self.phase.project_activity(activity)

    @property
    @abstractmethod
    def executing(self) -> bool: ...

    @property
    @abstractmethod
    def report_turn(self) -> str: ...

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
    def observed_id(self, finished: str | None) -> str | None: ...

    @abstractmethod
    def started_by(self, updated_at_ms: int) -> bool: ...

    @abstractmethod
    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]: ...

    @abstractmethod
    def matches(self, turn_id: TurnId) -> bool: ...


@dataclass(frozen=True)
class NoTurn(NullMember, TurnDeclaration):
    phase: ClassVar[TurnPhase] = IdlePhase()
    id = None
    started_at = None
    executing = False
    report_turn = ""

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

    def observed_id(self, finished: str | None) -> str | None:
        return finished

    def started_by(self, updated_at_ms: int) -> bool:
        return False

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]:
        return iter(())

    def matches(self, turn_id: TurnId) -> bool:
        return False


@dataclass(frozen=True)
class ActiveTurn(RecordMember, TurnDeclaration):
    id: str
    owner_pid: int
    started_at: float = field(default_factory=time.time)
    routing: TurnRouting | None = None
    claimed_generation: int | None = field(default=None, metadata={"wire_name": "admission_generation"})
    turn_generation: int | None = None
    phase: TurnPhase = field(default_factory=PreparingPhase)
    executing = True

    @property
    def report_turn(self) -> str:
        return self.id

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

    def held_by(self, lease: TurnLeaseFence) -> bool:
        return self.id == lease.turn_id and self.current(
            lease.claimed_generation, lease.identity.generation)

    def may_release(self, lease: TurnLeaseFence) -> bool:
        return self.held_by(lease) or (
            self.claimed_generation is None
            and replace(self, claimed_generation=lease.claimed_generation).held_by(lease))

    def lease(
        self, incarnation: ThreadIncarnation, record: StoredTurnRecord,
    ) -> TurnLeaseFence | None:
        if self.claimed_generation is None or self.turn_generation is None:
            return None
        return TurnLeaseFence(TurnIdentity(incarnation, self.turn_generation),
                              self.id, self.claimed_generation, record=record)

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


@dataclass(frozen=True, slots=True)
class TurnLeaseFence(TurnFence):
    """Exact local begin-turn lease and its original record."""

    record: StoredTurnRecord = field(kw_only=True)


class TurnRecord(SubclassUnion):
    """A stored turn reference or the declared absence of one; never a phase cache."""

    @abstractmethod
    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> TurnRecord: ...

    @abstractmethod
    def read(self) -> TurnDeclaration: ...

    @property
    def executing(self) -> bool:
        return self.read().executing

    @abstractmethod
    def without_claim(self) -> None: ...

    @abstractmethod
    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> TurnLeaseFence | None: ...


@dataclass(frozen=True)
class NoTurnRecord(NullMember, TurnRecord):
    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> NoTurnRecord:
        return self

    def read(self) -> NoTurn:
        return NoTurn()

    def without_claim(self) -> None:
        pass

    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> TurnLeaseFence | None:
        return None


@dataclass(frozen=True)
class StoredTurnReference(RecordMember, TurnRecord):
    """Wire presence only; the registry's thread supplies the turn identity."""

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:
        return StoredTurnRecord.for_identity(registry_path, identity)

    def read(self) -> TurnDeclaration:
        raise RelationViolationError("Turn reference must be bound by its registry")

    def without_claim(self) -> None:
        self.read()

    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> TurnLeaseFence | None:
        raise RelationViolationError("Turn reference must be bound by its registry")


@dataclass(frozen=True)
class StoredTurnRecord(JsonDocument[ActiveTurn], StoredTurnReference):
    """Turn file bound to the registry that decoded its wire reference."""

    identity: TurnIdentity = field(metadata={"wire_transient": True})
    registry_path: Path = field(repr=False, compare=False, metadata={"wire_transient": True})

    record_type = ActiveTurn

    @property
    def path(self) -> Path:  # type: ignore[override]  # Derived from its registry.
        identity = self.identity
        name = f"{identity.incarnation.require_recorded().lookup}-{identity.generation}.json"
        return self.registry_path.parent / "turns" / name

    def bound_to(self, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:
        record = super().bound_to(registry_path, identity)
        return (self if (self.registry_path, self.identity) ==
                (record.registry_path, record.identity) else record)

    def empty(self) -> ActiveTurn:
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

    def lease(self, incarnation: ThreadIncarnation, process: ProcessState,
              generation: int) -> TurnLeaseFence | None:
        active = self.read()
        active.validate_owner(process, generation)
        return active.lease(incarnation, self)

    def without_claim(self) -> None:
        self._publish_unlocked(self.read().without_claim())

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


class FinishedTurnFence(TurnFence):
    """Durable exact-turn completion witness, not a reply or model grant."""
