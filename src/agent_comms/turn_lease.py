"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar

from .activity import ObservedActivity
from .errors import RelationViolationError
from .routing import TurnRouting
from .field_codec import FieldCodec, SubclassUnion
from .thread_identity import ThreadIncarnation, TurnId, TurnIdentity
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
    def lease(self, incarnation: ThreadIncarnation) -> TurnLeaseFence | None: ...

    @abstractmethod
    def observed_id(self, finished: str | None) -> str | None: ...

    @abstractmethod
    def started_by(self, updated_at_ms: int) -> bool: ...

    @abstractmethod
    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]: ...

    @abstractmethod
    def matches(self, turn_id: TurnId) -> bool: ...


@dataclass(frozen=True)
class NoTurn(TurnDeclaration):
    phase: ClassVar[TurnPhase] = IdlePhase()
    id = None
    started_at = None
    executing = False
    report_turn = ""

    @classmethod
    def from_wire(cls, data: object) -> NoTurn:
        FieldCodec.decode(type(None), data)
        return cls()

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(type(None))

    def to_wire(self) -> None:
        return None

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

    def lease(self, incarnation: ThreadIncarnation) -> TurnLeaseFence | None:
        return None

    def observed_id(self, finished: str | None) -> str | None:
        return finished

    def started_by(self, updated_at_ms: int) -> bool:
        return False

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]:
        return iter(())

    def matches(self, turn_id: TurnId) -> bool:
        return False


@dataclass(frozen=True)
class ActiveTurn(TurnDeclaration):
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

    def lease(self, incarnation: ThreadIncarnation) -> TurnLeaseFence | None:
        if self.claimed_generation is None or self.turn_generation is None:
            return None
        return TurnLeaseFence(TurnIdentity(incarnation, self.turn_generation),
                              self.id, self.claimed_generation)

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

    @classmethod
    def from_wire(cls, data: Mapping) -> ActiveTurn:
        return FieldCodec.decode_record(cls, data)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.record_schema(cls)

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode_record(self)


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
class TurnFence:
    """One exact turn identity and its recorded input generation."""

    identity: TurnIdentity
    turn_id: str
    claimed_generation: int = field(metadata={"wire_name": "admission_generation"})

    def renamed(self, name: str) -> TurnFence:
        """Registry alias resolution changes the name, never incarnation or epochs."""
        return replace(self, identity=replace(
            self.identity, incarnation=replace(self.identity.incarnation, name=name)
        ))

    def can_attest(self, claim_scope: int) -> bool:
        return (self.identity.generation > 0 and self.claimed_generation > 0
                and self.claimed_generation == claim_scope)

    def matches(self, other: TurnFence) -> bool:
        """Lease and finished observations may describe the same exact turn."""
        return (
            self.identity == other.identity
            and self.turn_id == other.turn_id
            and self.claimed_generation == other.claimed_generation
        )


class TurnLeaseFence(TurnFence):
    """Exact local begin-turn lease; a reused turn ID is not this lease."""


class FinishedTurnFence(TurnFence):
    """Durable exact-turn completion witness, not a reply or model grant."""
