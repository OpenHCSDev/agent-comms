"""Message-derived activity clocks, independent of bus storage and routing."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Self

from .field_codec import FieldCodec
from .typed_table import TypedRow

if TYPE_CHECKING:
    from .channels import Channel
    from .messages import Message


@dataclass(frozen=True, slots=True)
class ChannelActivity(TypedRow):
    last_message: float = 0
    last_user_input: float = 0

    def observe(self, message: Message) -> ChannelActivity:
        return ChannelActivity(
            max(self.last_message, message.timestamp),
            message.sender_role.last_user_input(self.last_user_input, message.timestamp),
        )

    @classmethod
    def from_message(cls, message: Message, **key: str) -> Self:
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

    @classmethod
    def for_views(cls, channels: Mapping[str, Channel],
                  by_target: Mapping[str, ChannelActivity]) -> Mapping[str, ChannelActivity]:
        return {name: cls.combine(channel.history_scope().target_values(by_target))
                for name, channel in channels.items()}


@dataclass(frozen=True)
class ActivityClocks:
    channels: dict[str, ChannelActivity] = field(default_factory=dict)
    sent: dict[str, float] = field(default_factory=dict)

