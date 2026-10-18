"""Disposable viewer metrics retain one declared scope and original bus boundary."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bus_activity_index import ChannelActivity
from .bus_projection import AppendCheckpoint, BusAppendIndex, NoCheckpoint
from .messages import Message
from .read_basis import ChannelDisplayScope, ReadCursor

if TYPE_CHECKING:
    from .wire_log import WireSnapshot

@dataclass(frozen=True)
class DisplayMetricScope:
    scopes: tuple[ChannelDisplayScope, ...]
    viewer_names: frozenset[str]
    seen: ReadCursor

    def unread(self, message: Message) -> bool:
        return message.sender not in self.viewer_names and message.seq not in self.seen

@dataclass(frozen=True)
class DisplaySnapshot:
    """Computed metrics, including zero rows, independent of a saved checkpoint."""

    semantics: DisplayMetricScope
    activity: dict[str, ChannelActivity]
    counts: dict[str, int]

    @staticmethod
    def empty(semantics: DisplayMetricScope) -> DisplaySnapshot:
        return DisplaySnapshot(semantics, {scope.channel: ChannelActivity() for scope in semantics.scopes},
                               dict.fromkeys((scope.channel for scope in semantics.scopes), 0))

    def observe(self, message: Message) -> None:
        unread = self.semantics.unread(message)
        for scope in self.semantics.scopes:
            if scope.includes(message):
                self.activity[scope.channel] = self.activity[scope.channel].observe(message)
                self.counts[scope.channel] += int(unread)


@dataclass(frozen=True)
class DisplayCheckpoint(DisplaySnapshot, AppendCheckpoint):
    schema: Literal[5] = field(default=5, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if any(value < 0 for value in self.counts.values()):
            raise ValueError("Unread projection count must be nonnegative")
        expected = {scope.channel for scope in self.semantics.scopes}
        if self.activity.keys() != expected or self.counts.keys() != expected:
            raise ValueError("Projection metrics differ from the captured channel scope")

    def matches_scope(self, semantics: DisplayMetricScope) -> bool:
        return self.semantics == semantics

    def current_for(self, opened: WireSnapshot, semantics: DisplayMetricScope) -> bool:
        return self.current_at(opened.revision) and self.matches_scope(semantics)

    @classmethod
    def from_prefix(cls, index: BusDisplayIndex, opened: WireSnapshot, boundary: int,
                    semantics: DisplayMetricScope, checkpoint: DisplayCheckpoint | NoCheckpoint) -> DisplayCheckpoint:
        if not checkpoint.matches_scope(semantics):
            snapshot = opened.query_page_index(lambda page: page.display_snapshot(semantics, boundary))
            offset = boundary
        else:
            snapshot = DisplaySnapshot(semantics, dict(checkpoint.activity), dict(checkpoint.counts))
            offset = checkpoint.offset
            for message, _ in opened.public_records(offset, before_offset=boundary):
                snapshot.observe(message)
        projected = cls(opened.revision, boundary, opened.fingerprint(boundary), semantics, snapshot.activity, snapshot.counts)
        if not checkpoint.matches_scope(semantics) or offset != boundary:
            index.write(projected)
        opened.remember_append_checkpoint(index, projected)
        return projected

class BusDisplayIndex(BusAppendIndex[DisplayCheckpoint]):
    record_type = DisplayCheckpoint

    def __init__(self, bus_path: Path, viewer: str):
        digest = hashlib.sha256(viewer.encode()).hexdigest()
        path = bus_path.with_name(f"bus_display_{self.record_type.schema}_{digest}.json")
        super().__init__(bus_path, path)

    def snapshot(self, opened: WireSnapshot, semantics: DisplayMetricScope) -> DisplaySnapshot:
        prefix = opened.complete_prefix
        checkpoint = opened.append_checkpoint(self)
        if checkpoint.current_for(opened, semantics):
            return checkpoint
        return prefix.project(self, opened, semantics, checkpoint)
