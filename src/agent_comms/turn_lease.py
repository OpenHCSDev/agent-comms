"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar

from .coordination_errors import StaleFence
from .errors import RelationViolationError
from .routing import TurnRouting
from .field_codec import FieldCodec, UnionDecoder, ValueDecoder, WireValue
from .thread_identity import RecordedThreadIncarnation, TurnId, TurnIdentity
from .turn_phase import IdlePhase, PreparingPhase, TurnPhase

if TYPE_CHECKING:
    from .child_process import ProcessState


class TurnDeclaration(WireValue):
    """The unchanged null-or-record lease, with behavior for each decoded state."""

    __slots__ = ()
    accepts_null = True
    external_json_shape = True
    phase: TurnPhase
    started_at: float | None
    _decoder: ClassVar[UnionDecoder[TurnDeclaration]]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        if TurnDeclaration in cls.__bases__:
            TurnDeclaration._decoder = ValueDecoder.for_members(tuple(TurnDeclaration.__subclasses__()))

    @classmethod
    def from_wire(cls, data: object) -> TurnDeclaration:
        return cls._decoder.decode(data)

    @classmethod
    def schema(cls):
        return FieldCodec.value_schema(cls._decoder.target)

    @abstractmethod
    def validate_owner(self, process: ProcessState, generation: int) -> None: ...

    @abstractmethod
    def require_idle(self, error: Exception) -> None: ...

    @abstractmethod
    def require_current(self, claim_scope: int, generation: int) -> None: ...

    @abstractmethod
    def require_active(self) -> ActiveTurn: ...

    @abstractmethod
    def matches_id(self, turn: TurnId) -> bool: ...

    @abstractmethod
    def claimed_by(self, claim_scope: int) -> bool: ...

    @abstractmethod
    def without_claim(self) -> TurnDeclaration: ...

    @abstractmethod
    def lease(self, incarnation: RecordedThreadIncarnation) -> TurnLeaseFence | None: ...

    @abstractmethod
    def observed_id(self, finished: str | None) -> str | None: ...

    @abstractmethod
    def started_by(self, updated_at_ms: int) -> bool: ...

    @abstractmethod
    def started_after(self, timestamp: float) -> bool: ...

    @abstractmethod
    def project_activity(self, activity): ...

    @abstractmethod
    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnDeclaration]: ...

    @property
    @abstractmethod
    def managed_id(self) -> str | None: ...

    @property
    @abstractmethod
    def report_turn(self) -> str: ...

    @property
    @abstractmethod
    def executing(self) -> bool: ...


@dataclass(frozen=True)
class NoTurn(TurnDeclaration):
    started_at = None

    @property
    def phase(self) -> TurnPhase:
        return IdlePhase()

    @classmethod
    def from_wire(cls, data):
        FieldCodec.decode(type(None), data)
        return cls()

    @classmethod
    def schema(cls):
        return FieldCodec.value_schema(type(None))

    def to_wire(self):
        return None

    def validate_owner(self, process, generation):
        pass

    def require_idle(self, error):
        pass

    def require_current(self, claim_scope, generation):
        pass

    def require_active(self):
        raise StaleFence("registry owner has no active turn")

    def matches_id(self, turn):
        return False

    def claimed_by(self, claim_scope):
        return False

    def without_claim(self):
        return self

    def lease(self, incarnation):
        return None

    def observed_id(self, finished):
        return finished

    def started_by(self, updated_at_ms):
        return False

    def started_after(self, timestamp):
        return False

    def project_activity(self, activity):
        return self.phase.project_activity(activity)

    def phase_effects(self, phase):
        return iter(())

    @property
    def managed_id(self):
        return None

    @property
    def report_turn(self):
        return ""

    @property
    def executing(self):
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

    def validate_owner(self, process: ProcessState, generation: int) -> None:
        if not process.owns_turn(self):
            raise RelationViolationError("A turn must belong to the registered executor.")
        if self.turn_generation is not None and (
            type(self.turn_generation) is not int
            or self.turn_generation <= 0
            or self.turn_generation != generation
        ):
            raise RelationViolationError("Active turn generation differs from its owner.")

    def require_idle(self, error):
        raise error

    def require_current(self, claim_scope, generation):
        if not self.current(claim_scope, generation):
            raise RelationViolationError("live owner turn claim_scope is no longer current")

    def require_active(self):
        return self

    def matches_id(self, turn):
        return TurnId(self.id) == turn

    def claimed_by(self, claim_scope):
        return self.claimed_generation == claim_scope

    def without_claim(self):
        return replace(self, claimed_generation=None)

    def lease(self, incarnation):
        if self.claimed_generation is None or self.turn_generation is None:
            return None
        return TurnLeaseFence(
            identity=TurnIdentity(incarnation, self.turn_generation),
            turn_id=self.id,
            claimed_generation=self.claimed_generation,
        )

    def observed_id(self, finished):
        return self.id

    def started_by(self, updated_at_ms):
        return self.started_at * 1000 <= updated_at_ms + 1

    def started_after(self, timestamp):
        return self.started_at > timestamp

    def project_activity(self, activity):
        return replace(self.phase.project_activity(activity), timestamp=max(activity.timestamp, self.started_at))

    def phase_effects(self, phase):
        if phase != self.phase:
            yield replace(self, phase=phase)

    @property
    def managed_id(self):
        return self.id

    @property
    def report_turn(self):
        return self.id

    @property
    def executing(self):
        return True

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
    def schema(cls):
        return FieldCodec.record_schema(cls)

    @classmethod
    def from_wire(cls, data: Mapping) -> ActiveTurn:
        return FieldCodec.decode_record(cls, data)

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode_record(self)


@dataclass(frozen=True)
class TurnState:
    """Read-only capture of the lease and finished ID, not another mutable store."""

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
        return replace(self, identity=replace(
            self.identity, incarnation=replace(self.identity.incarnation, name=name)
        ))

    def can_attest(self, claim_scope: int) -> bool:
        return self.identity.generation > 0 and self.claimed_generation > 0 and self.claimed_generation == claim_scope

    def matches(self, other: TurnFence) -> bool:
        return (
            self.identity == other.identity
            and self.turn_id == other.turn_id
            and self.claimed_generation == other.claimed_generation
        )


class TurnLeaseFence(TurnFence):
    """Exact local begin-turn lease; a reused turn ID is not this lease."""


class FinishedTurnFence(TurnFence):
    """Durable exact-turn completion witness, not a reply or model grant."""
