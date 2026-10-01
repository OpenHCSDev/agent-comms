"""Canonical owner attestation held through adaptive compaction mutations.

Registration rechecks process identity, generation, active turn and goal under
its lock. Native session fields are caller observations; the native writer CAS
validates them under the session fence. CompactionSource owns input/wire
correction evidence, independently of this registry receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import RelationViolationError
from .goals import AbsentGoalCheckpoint, GoalCheckpoint, GoalRevision, PresentGoalCheckpoint
from .thread_identity import GenerationCounter, TurnId

if TYPE_CHECKING:
    from .native_input_owner import RegistryOwner
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .registration import Registration

__all__ = ["OwnerCompactionAttestation"]


@dataclass(frozen=True)
class OwnerCompactionAttestation:
    """Evidence snapshot proving canonical owner authority at one instant.

    Projected by Thread and rechecked by Registration.guard_owner_compaction
    while holding the registry store lock. ``session_*`` fields are echoed caller values,
    not registry observations.
    """

    thread: str
    owner_generation: int = field(metadata={"wire_name": "owner_epoch"})
    turn_id: str
    goal_id: str | None
    goal_revision: int | None
    session_file: str
    session_leaf: str
    session_revision: str
    registry_revision: tuple[int, int, int, int] | None

    def __post_init__(self) -> None:
        GenerationCounter.require_positive(self.owner_generation)
        TurnId.for_registration(self.turn_id)
        if not self.session_file or not self.session_leaf or not self.session_revision:
            raise ValueError("Owner compaction requires complete native session evidence")
        if (self.goal_id is None) != (self.goal_revision is None):
            raise ValueError("Owner compaction requires one complete goal revision")
        self.goal_checkpoint

    @property
    def goal_checkpoint(self) -> GoalCheckpoint:
        if self.goal_id is None:
            return AbsentGoalCheckpoint()
        assert self.goal_revision is not None
        return PresentGoalCheckpoint(GoalRevision(self.goal_id, self.goal_revision))

    def require_current(
        self, owner: RegistryOwner, snapshot: RegistrySnapshot, expected: Thread
    ) -> None:
        if self.thread != expected.name:
            raise RelationViolationError("canonical owner thread changed")
        owner.require_exact(snapshot, expected, self.owner_generation)
        owner.thread.require_turn(TurnId(self.turn_id), owner.admission_generation)
        if owner.thread.goal_checkpoint != self.goal_checkpoint:
            raise RelationViolationError("canonical owner goal revision changed")

    def require_registry(self, registry: Registration, expected: Thread) -> None:
        """Recheck source custody without holding the registry during provider work."""
        with registry.guard_owner_compaction(expected, self):
            pass
