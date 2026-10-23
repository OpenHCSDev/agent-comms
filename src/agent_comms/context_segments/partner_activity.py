"""What changed in the goals of the peers a thread is kept informed about.

The changes are read from each peer's goal history when the thread's next
turn is prepared. Nothing is sent and nobody is woken.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import ClassVar

from ..field_codec import FieldCodec
from ..goal_history import GoalHistoryEntry
from ..goals import GoalPeerChange
from ..peer_relevance import Informed
from ..turn_context import InstructionSegment, OwnerProvenance


@dataclass(frozen=True)
class PartnerGoalChange(GoalPeerChange):
    """The latest recorded change to one peer's goal."""

    peer: str
    observed_at: float
    @classmethod
    def latest(cls, peer: str, history: tuple[GoalHistoryEntry, ...]):
        if not history:
            return None
        entry = history[-1]
        goal = entry.after
        values = goal.peer_change_values()
        return cls(peer=peer, observed_at=entry.observed_at,
                   goal=values.goal, state=values.state, progress=values.progress)


@dataclass(frozen=True, kw_only=True)
class PartnerActivitySegment(InstructionSegment):
    changes: tuple[PartnerGoalChange, ...]
    LATEST: ClassVar[int] = 4

    @classmethod
    def capture(cls, owner, relations, goals):
        """The most recent goal change of each peer whose relevance keeps ``owner`` informed."""
        changes = tuple(
            change
            for relation in relations
            if relation.relevance.grants(Informed)
            for change in (
                PartnerGoalChange.latest(relation.peer.name, goals.goal_history(relation.peer.name)),
            )
            if change is not None
        )
        if not changes:
            return ()
        changes = tuple(sorted(changes, key=lambda change: change.observed_at)[-cls.LATEST:])
        revision = hashlib.sha256(json.dumps(FieldCodec.encode(changes)).encode()).hexdigest()
        return (super().capture((OwnerProvenance(owner.incarnation, revision),), changes=changes),)

    def values(self):
        return {"changes": FieldCodec.compact(FieldCodec.encode(self.changes))}
