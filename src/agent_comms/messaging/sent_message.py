"""A reported send and a source-proved publication have different authority.

SentTranscript remains the existing wire payload. These values are local read
results, not another wire format or a change to native tool result producers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.transcript.transcript_events import SentTranscript


class PublicationSource(ABC):
    """Where a publication came from; each member answers sameness for its own kind."""

    @abstractmethod
    def same_publication_source(self, other: PublicationSource) -> bool: ...

    def same_as_assigned(self, assigned: AssignedSourceIdentity) -> bool:
        return False

    def same_as_unassigned(self) -> bool:
        return False


class NoAssignedSource(PublicationSource):
    """A publication no wire recipient was assigned to."""

    def same_publication_source(self, other: PublicationSource) -> bool:
        return other.same_as_unassigned()

    def same_as_unassigned(self) -> bool:
        return True


NO_ASSIGNED_SOURCE = NoAssignedSource()


@dataclass(frozen=True)
class AssignedSourceIdentity(PublicationSource):
    root: str
    recipient: ThreadIncarnation

    @property
    def publication_key(self) -> tuple[str, float]:
        """Published authorship survives a rename, not a new birth or wire."""
        return self.root, self.recipient.require_recorded().created_at

    def same_publication_source(self, other: PublicationSource) -> bool:
        return other.same_as_assigned(self)

    def same_as_assigned(self, assigned: AssignedSourceIdentity) -> bool:
        return self.root == assigned.root and self.recipient == assigned.recipient


@dataclass(frozen=True, eq=False)
class OutgoingMessageReference:
    """The original wire, sender birth and exact message; rename is not reuse."""

    scope: AssignedSourceIdentity
    message: MessageReference

    def __post_init__(self):
        self.scope.recipient.require_recorded()

    def __hash__(self):
        return hash((self.scope.publication_key, self.message))

    def __eq__(self, other):
        if not isinstance(other, OutgoingMessageReference):
            return NotImplemented
        return (self.scope.same_publication_source(other.scope)
                and self.message == other.message)


@dataclass(frozen=True)
class PublishedSentMessage:
    """A full reported message proved by its original sender's wire assignment."""

    original: Message
    scope: AssignedSourceIdentity

    def __post_init__(self):
        self.scope.recipient.require_recorded()

    @classmethod
    def from_originals(
        cls, message: SentTranscript, scope: AssignedSourceIdentity,
        originals: Mapping[MessageReference, Message],
    ) -> Self | SentTranscript:
        """Only an exact original qualifies a report as a published message."""
        try:
            original = originals[message.source]
        except KeyError:
            return message
        return cls(original, scope) if message.matches(original) else message

    @property
    def message(self) -> SentTranscript:
        """The report is derived from the retained original that qualified it."""
        return SentTranscript.from_message(self.original)

    @property
    def reference(self) -> OutgoingMessageReference:
        return OutgoingMessageReference(self.scope, self.message.source)
