"""Message-derived activity clocks, independent of bus storage and routing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from typing import Self

from agent_comms.messaging.display_sources import SentClocks
from agent_comms.messaging.message_clock_source import MessageClockSource
from agent_comms.messaging.projection_source import ProducedRow, ProjectionSource
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.storage.typed_table import TypedRow


@dataclass(frozen=True, slots=True)
class ChannelActivity(TypedRow, ProjectionSource):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from agent_comms.messaging.bus_activity_index import BusActivityIndex
        from agent_comms.messaging.bus_display_index import BusDisplayIndex
        return (BusActivityIndex, BusDisplayIndex)

    last_message: float = 0
    last_user_input: float = 0

    def observe(self, message: MessageClockSource) -> ChannelActivity:
        return ChannelActivity(
            max(self.last_message, message.timestamp),
            message.sender_role.last_user_input(self.last_user_input, message.timestamp),
        )

    @classmethod
    def from_message(cls, message: MessageClockSource, **key: str) -> Self:
        clocks = ChannelActivity(
            message.timestamp,
            message.sender_role.last_user_input(0.0, message.timestamp),
        )
        return FieldCodec.construct(cls, {
            **{item.name: getattr(clocks, item.name) for item in fields(clocks)},
            **key,
        })

    @classmethod
    def combine(cls, clocks: Iterable[ChannelActivity]) -> ChannelActivity:
        clocks = tuple(clocks)
        return cls(max((item.last_message for item in clocks), default=0),
                   max((item.last_user_input for item in clocks), default=0))



@dataclass(frozen=True)
class ActivityClocks(SentClocks):
    channels: dict[str, ChannelActivity] = field(default_factory=dict)
    sent: dict[str, float] = field(default_factory=dict)
