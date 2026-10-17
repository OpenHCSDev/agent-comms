"""A reported send and a source-proved publication have different authority.

A report's publication is proved by its original sender's wire assignment.
The proved value retains that original Message, not a second report-shaped copy.
"""
from __future__ import annotations

from dataclasses import dataclass

from .messages import Message
from .message_reference import MessageReference
from .transcript_events import SentTranscript
from .transcript_receipts import AssignedSourceIdentity


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

    message: Message
    scope: AssignedSourceIdentity

    def __post_init__(self):
        self.scope.recipient.require_recorded()

    @property
    def reference(self) -> OutgoingMessageReference:
        return OutgoingMessageReference(self.scope, self.message.reference)

    @property
    def transcript(self) -> SentTranscript:
        return SentTranscript.from_message(self.message)
