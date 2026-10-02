"""Disposable viewer metrics retain one declared scope and original bus boundary."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex, BusFileRevision
from .read_basis import ChannelDisplayScope
from .read_ledger import ReadLedger
from .messages import Message

DisplayMetrics = tuple[dict[str, tuple[float, float]], dict[str, int]]


@dataclass(frozen=True)
class DisplayMetricScope:
    scopes: tuple[ChannelDisplayScope, ...]
    activity_scopes: tuple[ChannelDisplayScope, ...]
    viewer_names: frozenset[str]

    @property
    def empty_metrics(self) -> DisplayMetrics:
        return ({scope.channel: (0.0, 0.0) for scope in self.activity_scopes},
                dict.fromkeys((scope.channel for scope in self.scopes), 0))

    def observe_wire(self, record, metrics: DisplayMetrics) -> None:
        from .wire_record import WireRecord
        for message in WireRecord.public_from_wire(record).messages():
            self.observe(message, metrics)

    def observe(self, message: Message, metrics: DisplayMetrics) -> None:
        clocks, unread = metrics
        for scope in self.activity_scopes:
            if scope.includes(message):
                last_message, last_user = clocks[scope.channel]
                clocks[scope.channel] = (
                    max(last_message, message.timestamp),
                    max(last_user, message.timestamp)
                    if ReadLedger.human(message.sender_role) else last_user,
                )
        if message.sender not in self.viewer_names:
            for scope in self.scopes:
                if scope.unread(message):
                    unread[scope.channel] += 1


@dataclass(frozen=True)
class DisplayCheckpoint(AppendCheckpoint):
    semantics: DisplayMetricScope
    activity: dict[str, tuple[float, float]]
    counts: dict[str, int]
    schema: Literal[2] = field(default=2, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if any(value < 0 for value in self.counts.values()):
            raise ValueError("Unread projection count must be nonnegative")
        expected = ({scope.channel for scope in self.semantics.activity_scopes},
                    {scope.channel for scope in self.semantics.scopes})
        if (self.activity.keys(), self.counts.keys()) != expected:
            raise ValueError("Projection metrics differ from the captured channel scope")

    @property
    def metrics(self) -> DisplayMetrics:
        return self.activity, self.counts

    def current_for(self, revision: BusFileRevision, semantics: DisplayMetricScope) -> bool:
        return (self.source, self.semantics) == (revision, semantics)


class BusDisplayIndex(BusAppendIndex):
    record_type = DisplayCheckpoint

    def __init__(self, bus_path: Path, viewer: str):
        digest = hashlib.sha256(viewer.encode()).hexdigest()
        super().__init__(bus_path, bus_path.with_name(f"bus_display_{digest}.json"))

    def snapshot(self, revision: tuple[int, int, int, int] | None,
                 semantics: DisplayMetricScope, initial: DisplayMetrics,
                 apply: Callable[[Mapping, DisplayMetrics], None]) -> DisplayCheckpoint | None:
        if revision is None:
            return None
        source = BusFileRevision(*revision)
        try:
            stream = self.bus_path.open("rb")
        except FileNotFoundError:
            return None
        with stream:
            if not source.opened_by(stream):
                return None
            if source.size:
                stream.seek(source.size - 1)
                if stream.read(1) != b"\n":
                    return None
            checkpoint = self.checkpoint(stream, source)
            if checkpoint is None or checkpoint.semantics != semantics:
                metrics, offset = (dict(initial[0]), dict(initial[1])), 0
            else:
                metrics, offset = checkpoint.metrics, checkpoint.offset
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
                apply(record, metrics)
            projected = DisplayCheckpoint(source, source.size,
                         AppendCheckpoint.fingerprint(stream, source.size),
                         semantics, *metrics)
            if offset != source.size:
                with suppress(OSError):
                    self.write(projected)
            return projected
