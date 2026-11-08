"""A goal's original dependency identity, independent of execution presentation."""

from dataclasses import dataclass
from typing import Protocol

from .nominal.field_codec import ThreadLookup, WireValue
from .thread_identity import RecordedThreadIncarnation


class GoalWaitSenderRead(Protocol):
    @property
    def sender_lookup(self) -> ThreadLookup: ...


@dataclass(frozen=True, slots=True)
class GoalWaitTarget(WireValue):
    incarnation: RecordedThreadIncarnation

    def sent(self, original: GoalWaitSenderRead) -> bool:
        return original.sender_lookup == self.incarnation.lookup

    def to_wire(self) -> dict[str, str | float]:
        return self.incarnation.to_wire()

    @classmethod
    def from_wire(cls, data: dict[str, object]) -> GoalWaitTarget:
        return cls(RecordedThreadIncarnation.from_wire(data))

    @classmethod
    def schema(cls) -> dict:
        return RecordedThreadIncarnation.schema()
