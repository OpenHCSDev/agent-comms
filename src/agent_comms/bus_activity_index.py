"""Disposable activity projection of the original complete bus boundary."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex

if TYPE_CHECKING:
    from .channels import Channel
    from .messages import Message
    from .wire_log import WireSnapshot

ActivityFields = tuple[str, str, float, bool, bool]


@dataclass(frozen=True)
class ActivityClocks:
    channels: Mapping[str, ChannelActivity]
    sent: Mapping[str, float]


@dataclass(frozen=True)
class ActivityCheckpoint(AppendCheckpoint):
    channels: dict[str, tuple[float, float]]
    sent: dict[str, float]
    schema: Literal[3] = field(default=3, kw_only=True)

    @property
    def snapshot(self) -> ActivityClocks:
        return ActivityClocks(
            {name: ChannelActivity(*clock) for name, clock in self.channels.items()}, self.sent,
        )


class BusActivityIndex(BusAppendIndex[ActivityCheckpoint]):
    record_type = ActivityCheckpoint
    fsync_parent = True

    def __init__(self, bus_path: Path):
        super().__init__(bus_path, bus_path.with_name("bus_activity_latest.json"))

    def snapshot(self, opened: WireSnapshot,
                 parse: Callable[[Mapping[str, Any]], Iterable[ActivityFields]]) -> ActivityClocks:
        boundary = opened.complete_boundary
        checkpoint = opened.append_checkpoint(self)
        if checkpoint is None:
            channels, sent, offset = {}, {}, 0
        else:
            channels, sent = dict(checkpoint.channels), dict(checkpoint.sent)
            offset = checkpoint.offset
        if offset == boundary:
            return ActivityClocks({name: ChannelActivity(*clock) for name, clock in channels.items()}, sent)
        for record, _, _ in opened.rows(offset, boundary):
            for sender, target, timestamp, is_user, is_sent in parse(record):
                last_message, last_user = channels.get(target, (0.0, 0.0))
                channels[target] = (max(last_message, timestamp),
                                    max(last_user, timestamp) if is_user else last_user)
                if is_sent:
                    sent[sender] = max(sent.get(sender, 0.0), timestamp)
        projected = ActivityCheckpoint(opened.revision, boundary, opened.fingerprint(boundary), channels, sent)
        self.write(projected)
        opened.remember_append_checkpoint(self, projected)
        return projected.snapshot



@dataclass(frozen=True, slots=True)
class ChannelActivity:
    last_message: float = 0
    last_user_input: float = 0

    def observe(self, message: Message) -> ChannelActivity:
        return ChannelActivity(
            max(self.last_message, message.timestamp),
            (
                max(self.last_user_input, message.timestamp)
                if not message.sender_role.executable
                else self.last_user_input
            ),
        )

    @classmethod
    def for_views(cls, channels: Mapping[str, Channel],
                  by_target: Mapping[str, ChannelActivity]) -> Mapping[str, ChannelActivity]:
        """Combine target-owned clocks using each declaration's history membership."""
        result = {}
        for name, channel in channels.items():
            targets = channel.history_targets
            clocks = (tuple(by_target.values()) if targets is None
                      else tuple(by_target.get(target, cls()) for target in targets))
            result[name] = cls(max((item.last_message for item in clocks), default=0),
                               max((item.last_user_input for item in clocks), default=0))
        return result
