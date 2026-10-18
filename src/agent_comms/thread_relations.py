"""Mutual thread identities and the records supplied by their relations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .peer_relevance import GoalContactRelevance, PeerRelevance
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class RelationRevision:
    """An earlier note of a relation and when it was replaced or written."""

    note: str
    at: float


@dataclass(frozen=True, slots=True)
class RelationRecord:
    """One relation as a thread reads it: who made it, when, why and how it changed."""

    peer: str
    relevance: type[PeerRelevance]
    author: str
    created_at: float | None
    note: str
    history: tuple[RelationRevision, ...]


class MutualThreadRelation(ABC):
    """Shared identity behavior for explicit and goal-derived mutual visibility."""

    relevance: ClassVar[type[PeerRelevance]]
    source: ClassVar[str]
    note: str

    @abstractmethod
    def record(self, peer: ThreadIncarnation) -> RelationRecord: ...

    @property
    @abstractmethod
    def owner_incarnation(self) -> ThreadIncarnation: ...

    @property
    @abstractmethod
    def peer_incarnation(self) -> ThreadIncarnation: ...

    @property
    def pair_identity(self) -> frozenset[ThreadIncarnation]:
        return frozenset((self.owner_incarnation, self.peer_incarnation))

    def incident(self, thread: Thread) -> bool:
        return thread.incarnation in self.pair_identity

    def entry_goal_contacts(self) -> tuple[GoalDerivedContact, ...]:
        return ()

    def counterpart(self, thread: Thread) -> ThreadIncarnation | None:
        if self.owner_incarnation == thread.incarnation:
            return self.peer_incarnation
        if self.peer_incarnation == thread.incarnation:
            return self.owner_incarnation
        return None


@dataclass(frozen=True, slots=True)
class GoalDerivedContact(MutualThreadRelation):
    """Mutual visibility only; a mention never accepts work or dispatches a wake."""

    owner: str
    owner_created_at: float
    peer: str
    peer_created_at: float
    goal_id: str
    text_revision: int
    relevance: ClassVar[type[PeerRelevance]] = GoalContactRelevance
    source: ClassVar[str] = "goal_mention"

    def entry_goal_contacts(self) -> tuple[GoalDerivedContact, ...]:
        return (self,)

    @property
    def note(self) -> str:
        return (
            f"Mentioned by {self.owner}'s goal {self.goal_id} "
            f"text revision {self.text_revision} (awareness only)"
        )

    def record(self, peer: ThreadIncarnation) -> RelationRecord:
        return RelationRecord(peer.name, self.relevance, self.owner, None, self.note, ())

    @property
    def owner_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created_at)

    @property
    def peer_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.peer, self.peer_created_at)
