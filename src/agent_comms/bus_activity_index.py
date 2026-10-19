"""Disposable activity projection of the original complete bus boundary."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex

if TYPE_CHECKING:
    from .channels import Channel
    from .messages import Message
    from .wire_log import WireSnapshot

@dataclass(frozen=True)
class ActivityClocks:
    channels: dict[str, ChannelActivity] = field(default_factory=dict)
    sent: dict[str, float] = field(default_factory=dict)

    def observe(self, message: Message) -> None:
        self.channels[message.target] = self.channels.get(message.target, ChannelActivity()).observe(message)
        if message.role.counts_as_conversation():
            self.sent[message.sender] = max(self.sent.get(message.sender, 0.0), message.timestamp)


@dataclass(frozen=True)
class ActivityCheckpoint(ActivityClocks, AppendCheckpoint):
    schema: Literal[4] = field(default=4, kw_only=True)

    def restore_into(self, clocks):
        clocks.channels.update(self.channels)
        clocks.sent.update(self.sent)
        return self.offset


class BusActivityIndex(BusAppendIndex[ActivityCheckpoint]):
    record_type = ActivityCheckpoint
    fsync_parent = True

    def __init__(self, bus_path: Path):
        super().__init__(bus_path, bus_path.with_name(f"bus_activity_{self.record_type.schema}.json"))

    def snapshot(self, opened: WireSnapshot) -> ActivityClocks:
        boundary = opened.complete_prefix.boundary
        checkpoint = opened.append_checkpoint(self)
        clocks = ActivityClocks()
        offset = checkpoint.restore_into(clocks)
        if offset == boundary:
            return clocks
        for message, _ in opened.public_records(offset, before_offset=boundary):
            clocks.observe(message)
        projected = ActivityCheckpoint(opened.revision, boundary, opened.fingerprint(boundary), clocks.channels, clocks.sent)
        self.write(projected)
        opened.remember_append_checkpoint(self, projected)
        return clocks



@dataclass(frozen=True, slots=True)
class ChannelActivity:
    last_message: float = 0
    last_user_input: float = 0

    def observe(self, message: Message) -> ChannelActivity:
        return ChannelActivity(
            max(self.last_message, message.timestamp),
            message.sender_role.last_user_input(self.last_user_input, message.timestamp),
        )

    @classmethod
    def for_views(cls, channels: Mapping[str, Channel],
                  by_target: Mapping[str, ChannelActivity]) -> Mapping[str, ChannelActivity]:
        """Combine target-owned clocks using each declaration's history membership."""
        result = {}
        for name, channel in channels.items():
            clocks = tuple(channel.history_scope().target_values(by_target))
            result[name] = cls(max((item.last_message for item in clocks), default=0),
                               max((item.last_user_input for item in clocks), default=0))
        return result
