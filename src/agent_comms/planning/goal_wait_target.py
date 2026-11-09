"""A goal's original dependency identity, independent of execution presentation."""

from dataclasses import dataclass

from agent_comms.nominal.field_codec import ThreadLookup, WireValue
from agent_comms.registry.thread_identity import RecordedThreadIncarnation


@dataclass(frozen=True, slots=True)
class GoalWaitTarget(WireValue):
    incarnation: RecordedThreadIncarnation

    def sent(self, sender_lookup: ThreadLookup) -> bool:
        return sender_lookup == self.incarnation.lookup

    def to_wire(self) -> dict[str, str | float]:
        return self.incarnation.to_wire()

    @classmethod
    def from_wire(cls, data: dict[str, object]) -> GoalWaitTarget:
        return cls(RecordedThreadIncarnation.from_wire(data))

    @classmethod
    def schema(cls) -> dict:
        return RecordedThreadIncarnation.schema()
