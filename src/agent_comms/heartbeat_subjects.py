"""What one heartbeat line is about, and the recorded Core action that disposes of it.

Ordinary actions dispose only of lines that began before them. Exact-head supersession
retires that lost head itself, even when an older observation is published later. Nothing
the commander says about a line counts unless Core recorded the relevant subject action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import MessageId
from agent_comms.head_review import PullRequestHead
from agent_comms.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.service_passes import ServicePasses, ServicePassStore
from agent_comms.thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from agent_comms.todos import HeadVerdict, StoredHeadReview

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.plan_phases import OrderPhase
    from agent_comms.registry_document import RegistrySnapshot


@dataclass(frozen=True)
class CommanderHistory:
    """One reader's actors, shared by every subject it evaluates; each subject reads only the
    record that disposes of it."""

    comms: Comms
    snapshot: RegistrySnapshot
    identities: frozenset[ThreadIncarnation]
    _actions: dict[HeartbeatSubject, EventTime] = field(default_factory=dict, init=False, compare=False)

    def action_at(self, subject: HeartbeatSubject) -> EventTime:
        """One original action per subject and captured history, independent of ping age."""
        if subject not in self._actions:
            self._actions[subject] = subject.action_at(self)
        return self._actions[subject]

    @cached_property
    def passes(self) -> ServicePasses:
        return ServicePassStore.at(self.comms.root).read()

    @classmethod
    def read(cls, comms: Comms, actors: frozenset[str]) -> Self:
        snapshot = comms.registry.snapshot()
        return cls(comms, snapshot, frozenset(snapshot.require(actor).incarnation for actor in actors))

    @cached_property
    def actors(self) -> frozenset[str]:
        return frozenset(actor.name for actor in self.identities)

    @cached_property
    def phases_checked(self) -> Mapping[type[OrderPhase], EventTime]:
        """When any actor last checked an order serving each phase."""
        return MappingProxyType({phase: RecordedEventTime(at) for phase, at
                                 in self.comms.goals.todos.phases_checked(self.identities).items()})


@dataclass(frozen=True)
class HeartbeatSubject(DeclaredFamily, affix="Subject"):

    def action_at(self, history: CommanderHistory) -> EventTime:
        """Subjects without a disposing action never invent one."""
        return UnrecordedEventTime()

    def disposed(self, history: CommanderHistory, since: float) -> bool:
        """Original action evidence is shared, while each line retains its own start."""
        return history.action_at(self).since(since)


@dataclass(frozen=True)
class UnitSubject(HeartbeatSubject):
    unit: ThreadIncarnation

    def action_at(self, history: CommanderHistory) -> EventTime:
        return max((RecordedEventTime(message.timestamp)
                    for message in history.comms.bus.bus.latest_addressed(
                        history.actors, history.snapshot.names_for(self.unit.name))),
                   key=lambda at: at.latest_with(float('-inf')), default=UnrecordedEventTime())


@dataclass(frozen=True)
class HeadSubject(HeartbeatSubject):
    request: PullRequestHead

    def clears(self, verdict: HeadVerdict) -> bool:
        return verdict.decides()

    def action_at(self, history: CommanderHistory) -> EventTime:
        return max((review.at for review in StoredHeadReview.head_reviews(
                        history.comms.goals.todos, self.request) if self.clears(review.verdict)),
                   key=lambda at: at.latest_with(float('-inf')), default=UnrecordedEventTime())


@dataclass(frozen=True)
class UnlandedHeadSubject(HeadSubject):
    """Exact-head supersession retires the lost work, not merely an older alarm."""

    def clears(self, verdict: HeadVerdict) -> bool:
        return verdict.supersedes()

    def disposed(self, history: CommanderHistory, since: float) -> bool:
        return history.action_at(self).occurred()


@dataclass(frozen=True)
class PassSubject(HeartbeatSubject):
    """The commander's heartbeat pass: a pass that completes after the line answers it."""

    commander: RecordedThreadIncarnation

    def action_at(self, history: CommanderHistory) -> EventTime:
        return history.passes.heartbeat_completed_at(self.commander)


@dataclass(frozen=True)
class AnswerSubject(HeartbeatSubject):
    """A message owed an answer: an action naming it with --answers acts on it."""

    message: MessageId
