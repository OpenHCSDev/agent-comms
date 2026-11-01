"""A reported send and a source-proved publication have different authority.

SentTranscript remains the existing wire payload. These values are local read
results, not another wire format or a change to native tool result producers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .message_reference import MessageReference
from .thread_identity import ThreadIncarnation
from .transcript_events import SentTranscript

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .messages import Message


@dataclass(frozen=True)
class AssignedSourceIdentity:
    root: str
    recipient: ThreadIncarnation

    @property
    def publication_key(self) -> tuple[str, float]:
        """Published authorship survives a rename, not a new birth or wire."""
        return self.root, self.recipient.require_recorded().created_at

    def same_publication_source(self, other: AssignedSourceIdentity) -> bool:
        return self.root == other.root and self.recipient == other.recipient


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

    message: SentTranscript
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
        return cls(message, scope) if message.matches(original) else message

    @property
    def reference(self) -> OutgoingMessageReference:
        return OutgoingMessageReference(self.scope, self.message.source)
