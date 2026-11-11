"""What one heartbeat line is about, and the recorded Core action that disposes of it.

Ordinary actions dispose only of lines that began before them. Exact-head supersession
retires that lost head itself, even when an older observation is published later. Nothing
the teammate says about a line counts unless Core recorded the relevant subject action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Mapping, Self

from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import ServicePasses, ServicePassStore
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import MessageId
from agent_comms.planning.order_phase import OrderPhase
from agent_comms.planning.orders import HeadVerdict, StoredHeadReview
from agent_comms.planning.owner_requests import OwnerRequestId
from agent_comms.planning.plan_phase_base import PlanPhase
from agent_comms.projects.head_review import PullRequestHead
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from agent_comms.storage.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.team.team_service import TeamService


@dataclass(frozen=True)
class TeamHistory:
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
        return cls.of(comms, comms.registry.snapshot(), actors)

    @classmethod
    def of_team(cls, comms: Comms) -> Self:
        """The team's executable collaborators, read from the same snapshot this history holds:
        one registry read, where computing the actors first read it twice."""
        snapshot = comms.registry.snapshot()
        return cls.of(comms, snapshot, TeamService.actors(snapshot))

    @classmethod
    def of(cls, comms: Comms, snapshot: RegistrySnapshot, actors: frozenset[str]) -> Self:
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

    def action_at(self, history: TeamHistory) -> EventTime:
        """Subjects without a disposing action never invent one."""
        return UnrecordedEventTime()

    def disposed(self, history: TeamHistory, since: float) -> bool:
        """Original action evidence is shared, while each line retains its own start."""
        return history.action_at(self).since(since)

    def threads(self) -> tuple[ThreadIncarnation, ...]:
        """The threads this subject is about; most subjects name none."""
        return ()


@dataclass(frozen=True)
class TeammateSubject(HeartbeatSubject, declared_name="unit"):
    unit: ThreadIncarnation

    def threads(self) -> tuple[ThreadIncarnation, ...]:
        return (self.unit,)

    def action_at(self, history: TeamHistory) -> EventTime:
        return max((RecordedEventTime(message.timestamp)
                    for message in history.comms.bus.bus.latest_addressed(
                        history.actors, history.snapshot.names_for(self.unit.name))),
                   key=lambda at: at.latest_with(float('-inf')), default=UnrecordedEventTime())


@dataclass(frozen=True)
class HeadSubject(HeartbeatSubject):
    request: PullRequestHead

    def clears(self, verdict: HeadVerdict) -> bool:
        return verdict.decides()

    def action_at(self, history: TeamHistory) -> EventTime:
        return max((review.at for review in StoredHeadReview.head_reviews(
                        history.comms.goals.todos, self.request) if self.clears(review.verdict)),
                   key=lambda at: at.latest_with(float('-inf')), default=UnrecordedEventTime())


@dataclass(frozen=True)
class UnlandedHeadSubject(HeadSubject):
    """Exact-head supersession retires the lost work, not merely an older alarm."""

    def clears(self, verdict: HeadVerdict) -> bool:
        return verdict.supersedes()

    def disposed(self, history: TeamHistory, since: float) -> bool:
        return history.action_at(self).occurred()


@dataclass(frozen=True)
class PassSubject(HeartbeatSubject):
    """The teammate's heartbeat pass: a pass that completes after the line answers it."""

    teammate: RecordedThreadIncarnation

    def action_at(self, history: TeamHistory) -> EventTime:
        return history.passes.heartbeat_completed_at(self.teammate)

    def threads(self) -> tuple[ThreadIncarnation, ...]:
        return (self.teammate,)


@dataclass(frozen=True)
class AnswerSubject(HeartbeatSubject):
    """A message owed an answer: an action naming it with --answers acts on it."""

    message: MessageId


@dataclass(frozen=True)
class RequestSubject(HeartbeatSubject):
    """Tristan's open request. An order serving it, or marking it done, ends the stalled line's
    condition itself, so no separate action record answers it before then."""

    request: OwnerRequestId


@dataclass(frozen=True)
class PhaseSubject(HeartbeatSubject):
    """An order serving this phase answers its ready-without-an-owner line."""

    phase: type[PlanPhase]

    def disposed(self, history: TeamHistory, since: float) -> bool:
        return history.phases_checked.get(self.phase, UnrecordedEventTime()).since(since)
