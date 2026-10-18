"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated

from .activity import ObservedActivity
from .routing import TurnRouting
from .field_codec import FieldCodec, PathText
from .errors import RelationViolationError
from .locked_store import JsonDocument
from .store_files import _store_lock
from .thread_identity import TurnIdentity
from .turn_phase import IdlePhase, PreparingPhase, TurnPhase


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    id: str
    owner_pid: int
    started_at: float = field(default_factory=time.time)
    routing: TurnRouting | None = None
    claimed_generation: int | None = field(default=None, metadata={"wire_name": "admission_generation"})
    turn_generation: int | None = None
    phase: TurnPhase = field(default_factory=PreparingPhase)

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
        return FieldCodec.decode(cls, data)

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)


@dataclass(frozen=True)
class TurnState:
    """Read-only capture of the existing lease, not another lifecycle store."""

    active: ActiveTurn | None = None
    finished_turn_id: str | None = None

    @property
    def phase(self) -> TurnPhase:
        return self.active.phase if self.active is not None else IdlePhase()

    def project_activity(self, activity: ObservedActivity) -> ObservedActivity:
        """The turn start is activity even before its first phase observation."""
        projected = self.phase.project_activity(activity)
        if self.active is None:
            return projected
        return replace(projected, timestamp=max(activity.timestamp, self.active.started_at))

    def phase_effects(self, phase: TurnPhase) -> Iterator[TurnState]:
        """Emit only a changed active turn, retaining its original identity."""
        if self.active is not None and phase != self.active.phase:
            yield replace(self, active=replace(self.active, phase=phase))

    def native_phase_effects(self, phase: TurnPhase) -> Iterator[TurnState]:
        """The existing phase owns precedence over a native observation."""
        yield from self.phase_effects(self.phase.observed(phase))

    @property
    def report_turn(self) -> str:
        return self.active.id if self.active is not None else ""

    @property
    def managed_id(self) -> str | None:
        return self.active.id if self.active is not None else None

    @property
    def started_at(self) -> float | None:
        return self.active.started_at if self.active is not None else None

    def matches(self, turn_id: str | None) -> bool:
        return turn_id is None or (self.managed_id or self.finished_turn_id) == turn_id

    @property
    def busy(self) -> bool:
        return self.phase.busy

    @property
    def accepts_prompt(self) -> bool:
        return self.phase.accepts_prompt

    @property
    def accepts_followup(self) -> bool:
        return self.phase.accepts_followup

    @property
    def can_compact(self) -> bool:
        return self.phase.can_compact

    @property
    def activity(self) -> str:
        return self.phase.summary


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
        return self.identity.generation > 0 and self.claimed_generation > 0 and self.claimed_generation == claim_scope

    def matches(self, other: TurnFence) -> bool:
        """Lease and finished observations may describe the same exact turn."""
        return (
            self.identity == other.identity
            and self.turn_id == other.turn_id
            and self.claimed_generation == other.claimed_generation
        )


class TurnLeaseFence(TurnFence):
    """Exact local begin-turn lease; a reused turn ID is not this lease."""

    def record(self, registry_path: Path) -> TurnRecord:
        return TurnRecord.for_identity(registry_path, self.identity)


@dataclass(frozen=True, slots=True)
class TurnRecord(JsonDocument[ActiveTurn]):
    """One turn's original record, serialized by the existing registry lock.

    The registry contains only this reference. A phase write never reads or
    rewrites the registry or its guard; lease allocation and retirement remain
    registry transactions. Readers capture the atomic record, not a cache.
    """

    path: Annotated[Path, PathText]
    registry_path: Annotated[Path, PathText]

    @property
    def record_type(self) -> type[ActiveTurn]:
        return ActiveTurn

    def empty(self) -> ActiveTurn:
        raise RelationViolationError(f"Active turn record is missing: {self.path}")

    @classmethod
    def for_identity(cls, registry_path: Path, identity: TurnIdentity) -> TurnRecord:
        name = f"{identity.incarnation.require_recorded().lookup}-{identity.generation}.json"
        registry_path = registry_path.resolve()
        return cls(registry_path.parent / "turns" / name, registry_path)

    @classmethod
    def begin(cls, registry_path: Path, identity: TurnIdentity, turn: ActiveTurn) -> TurnRecord:
        """The allocating registry transaction owns the existing lock here."""
        record = cls.for_identity(registry_path, identity)
        record.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record._publish_unlocked(turn)
        return record

    def without_claim(self) -> None:
        """The revoking registry transaction owns the same lock."""
        self._publish_unlocked(replace(self.read(), claimed_generation=None))

    def _turn_effects(
        self, lease: TurnLeaseFence, observe: Callable[[TurnState], Iterable[TurnState]],
    ) -> tuple[TurnState, ...]:
        with _store_lock(self.registry_path):
            active = self.read()
            if active.id != lease.turn_id or not active.current(
                lease.claimed_generation, lease.identity.generation
            ):
                return ()
            effects = tuple(observe(TurnState(active)))
            for state in effects:
                assert state.active is not None
                self._publish_unlocked(state.active)
            return effects

    def transition(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        return self._turn_effects(lease, lambda state: state.phase_effects(phase))

    def observe_native_phase(
        self, lease: TurnLeaseFence, phase: TurnPhase,
    ) -> tuple[TurnState, ...]:
        return self._turn_effects(lease, lambda state: state.native_phase_effects(phase))


class FinishedTurnFence(TurnFence):
    """Durable exact-turn completion witness, not a reply or model grant."""
