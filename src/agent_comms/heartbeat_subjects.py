"""What one heartbeat line is about, and the recorded Core action that disposes of it.

A line is disposed only by a verb Core recorded on its subject after the line began: a
commander or fork message reaching the unit, or a verdict on the exact pull request head.
Nothing the commander says about the line counts.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.declared_family import DeclaredFamily
from agent_comms.head_review import PullRequestHead
from agent_comms.thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class HeartbeatSubject(DeclaredFamily, affix="Subject"):

    @abstractmethod
    def disposed(self, comms: Comms, actors: frozenset[str], since: float) -> bool:
        """Whether Core recorded an action on this subject at or after ``since``; ``actors`` are
        the names the commander and its forks publish under."""


@dataclass(frozen=True)
class UnitSubject(HeartbeatSubject):
    unit: ThreadIncarnation

    def disposed(self, comms: Comms, actors: frozenset[str], since: float) -> bool:
        return any(message.timestamp >= since for message in comms.bus.bus.latest_addressed(
            actors, comms.registry.snapshot().names_for(self.unit.name),
        ))


@dataclass(frozen=True)
class HeadSubject(HeartbeatSubject):
    request: PullRequestHead

    def disposed(self, comms: Comms, actors: frozenset[str], since: float) -> bool:
        return any(review.at.since(since) for review in comms.goals.todos.head_reviews(self.request))
