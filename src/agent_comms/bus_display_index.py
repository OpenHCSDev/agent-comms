"""Disposable viewer metrics retain one declared scope and original bus boundary."""
from __future__ import annotations

import hashlib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex, BusFileRevision
from .read_basis import ChannelDisplayScope
from .read_ledger import ReadLedger
from .messages import Message

if TYPE_CHECKING:
    from .wire_log import OpenedWireSnapshot

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

    def snapshot(self, opened: OpenedWireSnapshot,
                 semantics: DisplayMetricScope) -> DisplayCheckpoint | None:
        source, stream = opened.revision, opened.stream
        if source is None or stream is None or not source.opened_by(stream):
            return None
        if source.size:
            stream.seek(source.size - 1)
            if stream.read(1) != b"\n":
                return None
        checkpoint = self.checkpoint(stream, source)
        if checkpoint is None or checkpoint.semantics != semantics:
            metrics, offset = semantics.empty_metrics, 0
        else:
            metrics, offset = checkpoint.metrics, checkpoint.offset
        for message, _ in opened.public_records(offset):
            semantics.observe(message, metrics)
        projected = DisplayCheckpoint(source, source.size,
                     AppendCheckpoint.fingerprint(stream, source.size),
                     semantics, *metrics)
        if checkpoint is None or checkpoint.semantics != semantics or offset != source.size:
            with suppress(OSError):
                self.write(projected)
        return projected
