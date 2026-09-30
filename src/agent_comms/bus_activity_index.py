"""Disposable activity projection of the original complete bus boundary."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex, BusFileRevision

if TYPE_CHECKING:
    from .channels import Channel
    from .messages import Message

ActivityFields = tuple[str, str, float, bool, bool]
ActivitySnapshot = tuple[dict[str, tuple[float, float]], dict[str, float]]


@dataclass(frozen=True)
class ActivityCheckpoint(AppendCheckpoint):
    channels: dict[str, tuple[float, float]]
    sent: dict[str, float]
    schema: Literal[3] = field(default=3, kw_only=True)

    @property
    def snapshot(self) -> ActivitySnapshot:
        return self.channels, self.sent


class BusActivityIndex(BusAppendIndex):
    record_type = ActivityCheckpoint

    def __init__(self, bus_path: Path):
        super().__init__(bus_path, bus_path.with_name("bus_activity_latest.json"))

    def snapshot(self, revision: tuple[int, int, int, int] | None,
                 parse: Callable[[Mapping[str, Any]], ActivityFields]) -> ActivitySnapshot | None:
        if revision is None:
            return {}, {}
        source = BusFileRevision(*revision)
        with self.bus_path.open("rb") as stream:
            if not source.opened_by(stream):
                return None
            if source.size:
                stream.seek(source.size - 1)
                if stream.read(1) != b"\n":
                    return None
            checkpoint = self.checkpoint(stream, source)
            if checkpoint is None:
                channels, sent, offset = {}, {}, 0
            else:
                (channels, sent), offset = checkpoint.snapshot, checkpoint.offset
            if offset == source.size:
                return channels, sent
            stream.seek(offset)
            while stream.tell() < source.size:
                raw = stream.readline(source.size - stream.tell())
                if not raw.endswith(b"\n"):
                    return None
                if not raw.strip():
                    continue
                record = json.loads(raw)
                if not isinstance(record, Mapping):
                    raise ValueError("JSONL bus row must be an object")
                sender, target, timestamp, is_user, is_sent = parse(record)
                last_message, last_user = channels.get(target, (0.0, 0.0))
                channels[target] = (max(last_message, timestamp),
                                    max(last_user, timestamp) if is_user else last_user)
                if is_sent:
                    sent[sender] = max(sent.get(sender, 0.0), timestamp)
            with suppress(OSError):
                self.write(ActivityCheckpoint(source, source.size,
                           AppendCheckpoint.fingerprint(stream, source.size), channels, sent),
                           fsync_parent=True)
            return channels, sent


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
