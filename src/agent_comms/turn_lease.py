"""Turn lease: declaration and persistence owners."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from .routing import TurnRouting
from .thread_identity import TurnIdentity


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    id: str
    owner_pid: int
    started_at: float = field(default_factory=time.time)
    routing: TurnRouting | None = None
    admission_generation: int | None = None
    turn_generation: int | None = None

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
        return cls(
            data["id"],
            data["owner_pid"],
            data["started_at"],
            TurnRouting.from_wire(data["routing"]) if data.get("routing") else None,
            data.get("admission_generation"),
            data.get("turn_generation"),
        )

    def to_wire(self) -> dict[str, object]:
        return {**asdict(self), "routing": self.routing.to_wire() if self.routing else None}


@dataclass(frozen=True, slots=True)
class TurnFence:
    """One exact turn identity and its admission witness."""

    identity: TurnIdentity
    turn_id: str
    admission_generation: int


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
