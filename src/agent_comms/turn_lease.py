"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace

from .routing import TurnRouting
from .field_codec import FieldCodec
from .thread_identity import TurnIdentity
from .turn_phase import IdlePhase, PreparingPhase, TurnPhase


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    id: str
    owner_pid: int
    started_at: float = field(default_factory=time.time)
    routing: TurnRouting | None = None
    admission_generation: int | None = None
    turn_generation: int | None = None
    phase: TurnPhase = field(default_factory=PreparingPhase)

    def owned_by(self, pid: int, admission_generation: int) -> bool:
        """This turn's local process and registry admission witness agree."""
        return (
            self.owner_pid == pid
            and self.admission_generation == admission_generation
            and admission_generation > 0
        )

    def current(self, admission_generation: int, turn_generation: int) -> bool:
        """Compare persisted turn witnesses with the current registry authority."""
        return (
            self.admission_generation == admission_generation
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
    """One exact turn identity and its admission witness."""

    identity: TurnIdentity
    turn_id: str
    admission_generation: int


    def renamed(self, name: str) -> TurnFence:
        """Registry alias resolution changes the name, never incarnation or epochs."""
        return replace(self, identity=replace(
            self.identity, incarnation=replace(self.identity.incarnation, name=name)
        ))

    def can_attest(self, admission: int) -> bool:
        return self.identity.generation > 0 and self.admission_generation > 0 and self.admission_generation == admission

    def matches(self, other: TurnFence) -> bool:
        """Lease and finished observations may describe the same exact turn."""
        return (
            self.identity == other.identity
            and self.turn_id == other.turn_id
            and self.admission_generation == other.admission_generation
        )


class TurnLeaseFence(TurnFence):
    """Exact local begin-turn lease; a reused turn ID is not this lease."""


class FinishedTurnFence(TurnFence):
    """Durable exact-turn completion witness, not a reply or model grant."""
