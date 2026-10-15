"""Bus position and content identity are one reference; neither alone is proof."""

from dataclasses import dataclass

from .thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class AssignedSourceIdentity:
    root: str
    recipient: ThreadIncarnation


@dataclass(frozen=True, slots=True)
class MessageReference:
    seq: int
    message_id: str


@dataclass(frozen=True)
class OutgoingMessageReference:
    """A publication is identified within its captured root and thread source."""

    scope: AssignedSourceIdentity
    message: MessageReference
