"""The stable participant lookup shared by registry and coordinator identities."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .coordination_contracts import MAX_IDENTIFIER_CHARS
from .field_codec import MalformedRecord, ThreadLookup


class ParticipantIdentity(ABC):
    """Naming a participant does not grant a live generation or replay permission."""

    __slots__ = ()

    @staticmethod
    def require_text(value: str, label: str) -> None:
        if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_CHARS:
            raise MalformedRecord(f"{label} must be a bounded nonempty string")
        if not value.strip():
            raise MalformedRecord(f"{label} must not be whitespace")

    @classmethod
    def require_canonical(cls, value: str, label: str) -> None:
        cls.require_text(value, label)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise MalformedRecord(f"{label} must be a canonical thread name")

    @property
    @abstractmethod
    def lookup(self) -> ThreadLookup: ...


@dataclass(frozen=True, slots=True)
class FrozenRecipientIdentity(ParticipantIdentity):
    """Send-time participant identity, independent of bus projection writers."""

    recipient_lookup: ThreadLookup
    canonical_thread: str

    def __post_init__(self) -> None:
        self.require_text(self.recipient_lookup, "recipient_lookup")
        self.require_canonical(self.canonical_thread, "canonical_thread")

    @property
    def lookup(self) -> ThreadLookup:
        return self.recipient_lookup
