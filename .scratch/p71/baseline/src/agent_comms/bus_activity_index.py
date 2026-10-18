"""Disposable activity projection of the original complete bus boundary."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex
from .coordination_errors import StaleRevision
from .private_path import FileRevision
from .errors import RelationViolationError

if TYPE_CHECKING:
    from .channels import Channel
    from .messages import Message
    from .wire_log import OpenedWireSnapshot

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

    def current_for(self, revision: FileRevision) -> bool:
        return (self.source, self.offset) == (revision, revision.size)


class BusActivityIndex(BusAppendIndex[ActivityCheckpoint]):
    record_type = ActivityCheckpoint
    fsync_parent = True

    def __init__(self, bus_path: Path):
        super().__init__(bus_path, bus_path.with_name("bus_activity_latest.json"))

    def snapshot(self, opened: OpenedWireSnapshot,
                 parse: Callable[[Mapping[str, Any]], Iterable[ActivityFields]]) -> ActivitySnapshot:
        source, stream = opened.revision, opened.stream
        if source is None:
            return {}, {}
        if not source.opened_by(stream):
            raise StaleRevision("Activity source changed before its captured read")
        if source.size:
            stream.seek(source.size - 1)
            if stream.read(1) != b"\n":
                raise RelationViolationError("Activity source has an incomplete original row")
        checkpoint = opened.append_checkpoint(self)
        if checkpoint is None:
            channels, sent, offset = {}, {}, 0
        else:
            channels, sent = dict(checkpoint.channels), dict(checkpoint.sent)
            offset = checkpoint.offset
        if offset == source.size:
            return (checkpoint or ActivityCheckpoint(
                source, source.size, AppendCheckpoint.fingerprint(stream, source.size),
                channels, sent)).snapshot
        stream.seek(offset)
        while stream.tell() < source.size:
            raw = stream.readline(source.size - stream.tell())
            if not raw.endswith(b"\n"):
                raise RelationViolationError("Activity source has an incomplete original row")
            if not raw.strip():
                continue
            record = json.loads(raw)
            if not isinstance(record, Mapping):
                raise ValueError("JSONL bus row must be an object")
            for sender, target, timestamp, is_user, is_sent in parse(record):
                last_message, last_user = channels.get(target, (0.0, 0.0))
                channels[target] = (max(last_message, timestamp),
                                    max(last_user, timestamp) if is_user else last_user)
                if is_sent:
                    sent[sender] = max(sent.get(sender, 0.0), timestamp)
        projected = ActivityCheckpoint(source, source.size,
                    AppendCheckpoint.fingerprint(stream, source.size), channels, sent)
        opened.remember_append_checkpoint(self, projected)
        with suppress(OSError):
            self.write(projected)
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
