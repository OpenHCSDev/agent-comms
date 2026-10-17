"""Disposable viewer metrics retain one declared scope and original bus boundary."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex
from .messages import Message
from .private_path import FileRevision
from .read_basis import ChannelDisplayScope, ReadCursor
from .read_ledger import ReadLedger

if TYPE_CHECKING:
    from .wire_log import OpenedWireSnapshot

DisplayMetrics = tuple[dict[str, tuple[float, float]], dict[str, int]]


@dataclass(frozen=True)
class DisplayMetricScope:
    scopes: tuple[ChannelDisplayScope, ...]
    viewer_names: frozenset[str]
    seen: ReadCursor

    @property
    def empty_metrics(self) -> DisplayMetrics:
        return ({scope.channel: (0.0, 0.0) for scope in self.scopes},
                dict.fromkeys((scope.channel for scope in self.scopes), 0))

    def unread(self, message: Message) -> bool:
        return message.sender not in self.viewer_names and message.seq not in self.seen

    def observe(self, message: Message, metrics: DisplayMetrics) -> None:
        clocks, counts = metrics
        unread = self.unread(message)
        for scope in self.scopes:
            if scope.includes(message):
                last_message, last_user = clocks[scope.channel]
                clocks[scope.channel] = (
                    max(last_message, message.timestamp),
                    max(last_user, message.timestamp)
                    if ReadLedger.human(message.sender_role) else last_user,
                )
                counts[scope.channel] += int(unread)


@dataclass(frozen=True)
class EmptyDisplaySnapshot:
    """A captured absent bus has no messages, not an unavailable index answer."""

    semantics: DisplayMetricScope

    @property
    def metrics(self) -> DisplayMetrics:
        return self.semantics.empty_metrics


@dataclass(frozen=True)
class DisplayCheckpoint(AppendCheckpoint):
    semantics: DisplayMetricScope
    activity: dict[str, tuple[float, float]]
    counts: dict[str, int]
    schema: Literal[4] = field(default=4, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if any(value < 0 for value in self.counts.values()):
            raise ValueError("Unread projection count must be nonnegative")
        expected = {scope.channel for scope in self.semantics.scopes}
        if self.activity.keys() != expected or self.counts.keys() != expected:
            raise ValueError("Projection metrics differ from the captured channel scope")

    @property
    def metrics(self) -> DisplayMetrics:
        return self.activity, self.counts

    def current_for(self, revision: FileRevision, semantics: DisplayMetricScope) -> bool:
        return (self.source, self.semantics) == (revision, semantics)

class BusDisplayIndex(BusAppendIndex[DisplayCheckpoint]):
    record_type = DisplayCheckpoint

    def __init__(self, bus_path: Path, viewer: str):
        digest = hashlib.sha256(viewer.encode()).hexdigest()
        path = bus_path.with_name(f"bus_display_{self.record_type.schema}_{digest}.json")
        super().__init__(bus_path, path)

    def snapshot(self, opened: OpenedWireSnapshot,
                 semantics: DisplayMetricScope) -> DisplayCheckpoint | EmptyDisplaySnapshot:
        boundary = opened.complete_boundary
        source, stream = opened.revision, opened.stream
        if source is None:
            return EmptyDisplaySnapshot(semantics)
        checkpoint = opened.append_checkpoint(self)
        if checkpoint is not None and checkpoint.current_for(source, semantics):
            return checkpoint
        if checkpoint is None or checkpoint.semantics != semantics:
            metrics = opened.query_page_index(
                lambda index: index.display_metrics(semantics, boundary), empty=semantics.empty_metrics)
            offset = boundary
        else:
            metrics, offset = (dict(checkpoint.activity), dict(checkpoint.counts)), checkpoint.offset
        for message, _ in opened.query_page_index(
            lambda index: tuple(opened.indexed_records(index,
                index.offsets(lower=None, upper=None, targets=None, after_offset=offset,
                    descending=False, before_offset=boundary), before_offset=boundary)), empty=()):
            semantics.observe(message, metrics)
        projected = DisplayCheckpoint(source, boundary,
                     AppendCheckpoint.fingerprint(stream, boundary),
                     semantics, *metrics)
        if checkpoint is None or checkpoint.semantics != semantics or offset != boundary:
            self.write(projected)
        opened.remember_append_checkpoint(self, projected)
        return projected
