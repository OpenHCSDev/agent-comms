"""Bus position and content identity are one reference; neither alone is proof."""

from __future__ import annotations

from dataclasses import dataclass, field

from .thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class AssignedSourceIdentity:
    root: str
    recipient: ThreadIncarnation

    @property
    def publication_key(self) -> tuple[str, float]:
        """Wire authorship uses the immutable birth, not a retained name.

        This is the creation identity encoded by stable_thread_lookup at commit.
        It says nothing about an ACP producer or a frontend attachment lifetime.
        """
        return self.root, self.recipient.created_at

    def same_publication_source(self, other: AssignedSourceIdentity) -> bool:
        return self.recipient.created_at > 0 and self.publication_key == other.publication_key


@dataclass(frozen=True, slots=True)
class MessageReference:
    seq: int
    message_id: str


@dataclass(frozen=True)
class OutgoingMessageReference:
    """Publication equality survives rename; the recorded scope stays original."""

    scope: AssignedSourceIdentity = field(compare=False)
    message: MessageReference
    publisher_key: tuple[str, float] = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "publisher_key", self.scope.publication_key)
