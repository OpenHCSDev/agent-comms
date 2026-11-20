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

    def capturing(self, subjects: tuple[HeartbeatSubject, ...]) -> Self:
        """Capture each declared action-source family once for this immutable history cut."""
        subjects = tuple(
            subject for subject in dict.fromkeys(subjects)
            if subject not in self._actions
        )
        owners = tuple(dict.fromkeys(type(subject).actions_at for subject in subjects))
        for owner in owners:
            members = tuple(
                subject for subject in subjects
                if type(subject).actions_at is owner
            )
            self._actions.update(owner(self, members))
        return self

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

    @staticmethod
    def actions_at(
        history: TeamHistory, subjects: tuple[HeartbeatSubject, ...],
    ) -> dict[HeartbeatSubject, EventTime]:
        """Subjects with no recorded action source all derive the same absent answer."""
        return {subject: UnrecordedEventTime() for subject in subjects}

    def action_at(self, history: TeamHistory) -> EventTime:
        """A singleton derives from the same family owner as a complete captured batch."""
        return type(self).actions_at(history, (self,))[self]

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

    @staticmethod
    def actions_at(
        history: TeamHistory, subjects: tuple[HeartbeatSubject, ...],
    ) -> dict[HeartbeatSubject, EventTime]:
        names = {
            subject: history.snapshot.names_for(subject.unit.name)
            for subject in subjects
        }
        latest = history.comms.bus.bus.latest_addressed_sources(
            history.actors,
            frozenset(name for aliases in names.values() for name in aliases),
        )
        return {
            subject: (
                RecordedEventTime(source.timestamp)
                if (source := max(
                    (latest[name] for name in aliases if name in latest),
                    key=lambda item: item.seq,
                    default=None,
                )) is not None
                else UnrecordedEventTime()
            )
            for subject, aliases in names.items()
        }


@dataclass(frozen=True)
class HeadSubject(HeartbeatSubject):
    request: PullRequestHead

    def clears(self, verdict: HeadVerdict) -> bool:
        return verdict.decides()

    @staticmethod
    def actions_at(
        history: TeamHistory, subjects: tuple[HeartbeatSubject, ...],
    ) -> dict[HeartbeatSubject, EventTime]:
        requests = tuple(dict.fromkeys(subject.request for subject in subjects))
        with history.comms.goals.todos.snapshot() as db:
            reviews = StoredHeadReview.head_reviews_among(db, requests)
        return {
            subject: max(
                (review.at for review in reviews[subject.request]
                 if subject.clears(review.verdict)),
                key=lambda at: at.latest_with(float('-inf')),
                default=UnrecordedEventTime(),
            )
            for subject in subjects
        }


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

    @staticmethod
    def actions_at(
        history: TeamHistory, subjects: tuple[HeartbeatSubject, ...],
    ) -> dict[HeartbeatSubject, EventTime]:
        return {
            subject: history.passes.heartbeat_completed_at(subject.teammate)
            for subject in subjects
        }

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

    @staticmethod
    def actions_at(
        history: TeamHistory, subjects: tuple[HeartbeatSubject, ...],
    ) -> dict[HeartbeatSubject, EventTime]:
        return {
            subject: history.phases_checked.get(subject.phase, UnrecordedEventTime())
            for subject in subjects
        }
