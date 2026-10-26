"""What one heartbeat line is about, and the recorded Core action that disposes of it.

A line is disposed only by a verb Core recorded on its subject after the line began: a
commander or fork message reaching the unit, a verdict on the exact pull request head, or an
answer clearing the owed message. Nothing the commander says about the line counts.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, Self

from agent_comms.commander_service import CommanderService
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import MessageId
from agent_comms.head_review import PullRequestHead
from agent_comms.service_passes import ServicePassStore
from agent_comms.thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from agent_comms.todos import StoredHeadReview

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.heartbeat_frame import HeartbeatFrame
    from agent_comms.todos import Todo
    from agent_comms.ui_model.unit_state import UnitState


@dataclass(frozen=True)
class CommanderHistory:
    """One board read's order history and current conditions, never retained between reads."""

    comms: Comms
    orders: Mapping[ThreadIncarnation, tuple[Todo, ...]]
    commander: str
    actors: frozenset[str]

    @classmethod
    def read(cls, comms: Comms, commander: str) -> Self:
        snapshot = comms.registry.snapshot()
        actors = CommanderService.actors(snapshot, commander)
        identities = frozenset(snapshot.require(actor).incarnation for actor in actors)
        return cls(comms, MappingProxyType({
            actor: comms.goals.todos.history_for_commander(actor) for actor in identities
        }), commander, actors)

    @cached_property
    def frame(self) -> HeartbeatFrame:
        from agent_comms.heartbeat_frame import HeartbeatFrame
        from agent_comms.todos import ProgressRead

        return HeartbeatFrame.capture(self.comms, self.commander, ProgressRead(), self.comms.plans)

    @cached_property
    def observed(self) -> tuple[UnitState, ...]:
        from agent_comms.ui_model.unit_state import UnitState

        return UnitState.current(self.comms, self.commander, self.frame)


@dataclass(frozen=True)
class HeartbeatSubject(DeclaredFamily, affix="Subject"):

    @abstractmethod
    def disposed(self, history: CommanderHistory, since: float) -> bool:
        """Whether Core recorded an action on this subject at or after ``since``."""


@dataclass(frozen=True)
class UnitSubject(HeartbeatSubject):
    unit: ThreadIncarnation

    def disposed(self, history: CommanderHistory, since: float) -> bool:
        return any(message.timestamp >= since for message in history.comms.bus.bus.latest_addressed(
            history.actors, history.comms.registry.snapshot().names_for(self.unit.name),
        ))


@dataclass(frozen=True)
class HeadSubject(HeartbeatSubject):
    request: PullRequestHead

    def disposed(self, history: CommanderHistory, since: float) -> bool:
        return any(review.at.since(since) and review.verdict.decides()
                   for review in StoredHeadReview.head_reviews(history.comms.goals.todos, self.request))


@dataclass(frozen=True)
class UnlandedHeadSubject(HeadSubject):
    """Only the forge/Git condition clears this line; reviewing it does not land it."""

    def disposed(self, history: CommanderHistory, since: float) -> bool:
        return False

    def acted_on(self, acted: frozenset[HeartbeatSubject]) -> bool:
        return False


@dataclass(frozen=True)
class PassSubject(HeartbeatSubject):
    """The commander's heartbeat pass: a pass that completes after the line answers it."""

    commander: RecordedThreadIncarnation

    def disposed(self, history: CommanderHistory, since: float) -> bool:
        return ServicePassStore.at(history.comms.root).read().completed_since(self.commander, since)


@dataclass(frozen=True)
class AnswerSubject(HeartbeatSubject):
    """A message owed an answer: an action naming it with --answers acts on it."""

    message: MessageId

    def disposed(self, history: CommanderHistory, since: float) -> bool:
        """The answer clears the owed message itself, so the line leaves the next packet."""
        return False
