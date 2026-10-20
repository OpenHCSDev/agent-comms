"""Disposable viewer metrics retain one declared scope and original bus boundary."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bus_activity_index import ChannelActivity
from .locked_store import PatternFile
from .bus_projection import AppendCheckpoint, BusAppendIndex, NoCheckpoint
from .messages import Message
from .read_basis import ChannelDisplayScope

if TYPE_CHECKING:
    from .wire_log import WireSnapshot

@dataclass(frozen=True)
class DisplaySnapshot:
    """Computed metrics, including zero rows, independent of a saved checkpoint."""

    scopes: tuple[ChannelDisplayScope, ...]
    activity: dict[str, ChannelActivity]

    @staticmethod
    def empty(scopes: tuple[ChannelDisplayScope, ...]) -> DisplaySnapshot:
        return DisplaySnapshot(scopes, {scope.channel: ChannelActivity() for scope in scopes})

    def observe(self, message: Message) -> None:
        for scope in self.scopes:
            if scope.includes(message):
                self.activity[scope.channel] = self.activity[scope.channel].observe(message)


@dataclass(frozen=True)
class DisplayCheckpoint(DisplaySnapshot, AppendCheckpoint):
    schema: Literal[7] = field(default=7, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.activity.keys() != {scope.channel for scope in self.scopes}:
            raise ValueError("Projection metrics differ from the captured channel scope")

    def matches_scope(self, scopes: tuple[ChannelDisplayScope, ...]) -> bool:
        return self.scopes == scopes

    def current_for(self, opened: WireSnapshot, scopes: tuple[ChannelDisplayScope, ...]) -> bool:
        return self.current_at(opened.revision) and self.matches_scope(scopes)

    @classmethod
    def from_prefix(cls, index: BusDisplayIndex, opened: WireSnapshot, boundary: int,
                    scopes: tuple[ChannelDisplayScope, ...], checkpoint: DisplayCheckpoint | NoCheckpoint) -> DisplayCheckpoint:
        if not checkpoint.matches_scope(scopes):
            snapshot = opened.query_page_index(lambda page: page.display_snapshot(scopes, boundary))
            offset = boundary
        else:
            snapshot = DisplaySnapshot(scopes, dict(checkpoint.activity))
            offset = checkpoint.offset
            for message, _ in opened.public_records(offset, before_offset=boundary):
                snapshot.observe(message)
        projected = cls(opened.revision, boundary, opened.fingerprint(boundary), scopes, snapshot.activity)
        if not checkpoint.matches_scope(scopes) or offset != boundary:
            index.write(projected)
        opened.remember_append_checkpoint(index, projected)
        return projected

class BusDisplayIndex(PatternFile, BusAppendIndex[DisplayCheckpoint]):
    record_type = DisplayCheckpoint
    filename = f"bus_display_{DisplayCheckpoint.schema}_{{digest}}.json"

    def __init__(self, bus_path: Path, viewer: str):
        digest = hashlib.sha256(viewer.encode()).hexdigest()
        path = self.path_in(bus_path.parent, digest=digest)
        super().__init__(bus_path, path)

    def snapshot(self, opened: WireSnapshot, scopes: tuple[ChannelDisplayScope, ...]) -> DisplaySnapshot:
        prefix = opened.complete_prefix
        checkpoint = opened.append_checkpoint(self)
        if checkpoint.current_for(opened, scopes):
            return checkpoint
        return prefix.project(self, opened, scopes, checkpoint)
