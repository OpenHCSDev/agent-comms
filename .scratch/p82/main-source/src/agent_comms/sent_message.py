"""A reported send and a source-proved publication have different authority.

SentTranscript remains the existing wire payload. These values are local read
results, not another wire format or a change to native tool result producers.
"""
from __future__ import annotations

from dataclasses import dataclass

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

    message: SentTranscript
    scope: AssignedSourceIdentity

    def __post_init__(self):
        self.scope.recipient.require_recorded()

    @property
    def reference(self) -> OutgoingMessageReference:
        return OutgoingMessageReference(self.scope, self.message.source)
