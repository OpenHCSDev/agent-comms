"""Disposable viewer metrics retain one declared scope and original bus boundary."""
from __future__ import annotations

import hashlib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bus_projection import AppendCheckpoint, BusAppendIndex
from .messages import Message
from .private_path import FileRevision
from .read_basis import ChannelDisplayScope
from .read_ledger import ReadLedger

if TYPE_CHECKING:
    from .wire_log import OpenedWireSnapshot

DisplayMetrics = tuple[dict[str, tuple[float, float]], dict[str, int]]


@dataclass(frozen=True)
class DisplayMetricScope:
    scopes: tuple[ChannelDisplayScope, ...]
    viewer_names: frozenset[str]
    seen_sequences: frozenset[int]

    @property
    def empty_metrics(self) -> DisplayMetrics:
        return ({scope.channel: (0.0, 0.0) for scope in self.scopes},
                dict.fromkeys((scope.channel for scope in self.scopes), 0))

    def unread(self, message: Message) -> bool:
        return message.sender not in self.viewer_names and message.seq not in self.seen_sequences

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
class DisplayCheckpoint(AppendCheckpoint):
    semantics: DisplayMetricScope
    activity: dict[str, tuple[float, float]]
    counts: dict[str, int]
    schema: Literal[3] = field(default=3, kw_only=True)

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

    def rebase(self, opened: OpenedWireSnapshot,
               semantics: DisplayMetricScope) -> DisplayMetrics:
        """Reuse each answer only while its own inclusion history agrees."""
        metrics = semantics.empty_metrics
        clocks, counts = metrics
        previous_scopes = {scope.channel: scope for scope in self.semantics.scopes}
        rebuild, changed_reads = [], []
        same_reads = semantics.seen_sequences == self.semantics.seen_sequences
        for scope in semantics.scopes:
            previous = previous_scopes.get(scope.channel)
            if scope == previous:
                clocks[scope.channel] = self.activity[scope.channel]
                if semantics.viewer_names == self.semantics.viewer_names:
                    counts[scope.channel] = self.counts[scope.channel]
                    if not same_reads:
                        changed_reads.append(scope)
                    continue
            rebuild.append(scope)
        rebuilt = DisplayMetricScope(tuple(rebuild), semantics.viewer_names,
                                     semantics.seen_sequences)
        changed_sequences = (self.semantics.seen_sequences ^ semantics.seen_sequences
                             if changed_reads else frozenset())
        records = (opened.public_records(before_offset=self.offset)
                   if rebuild else
                   opened.public_records_for(changed_sequences, before_offset=self.offset))
        for message, _ in records:
            rebuilt.observe(message, metrics)
            delta = int(semantics.unread(message)) - int(self.semantics.unread(message))
            if delta:
                for scope in changed_reads:
                    if scope.includes(message):
                        counts[scope.channel] += delta
        return metrics


class BusDisplayIndex(BusAppendIndex[DisplayCheckpoint]):
    record_type = DisplayCheckpoint

    def __init__(self, bus_path: Path, viewer: str):
        digest = hashlib.sha256(viewer.encode()).hexdigest()
        path = bus_path.with_name(f"bus_display_{self.record_type.schema}_{digest}.json")
        super().__init__(bus_path, path)

    def snapshot(self, opened: OpenedWireSnapshot,
                 semantics: DisplayMetricScope) -> DisplayCheckpoint | None:
        source, stream = opened.revision, opened.stream
        if source is None or stream is None or not source.opened_by(stream):
            return None
        if source.size:
            stream.seek(source.size - 1)
            if stream.read(1) != b"\n":
                return None
        checkpoint = opened.append_checkpoint(self)
        if checkpoint is not None and checkpoint.current_for(source, semantics):
            return checkpoint
        if checkpoint is None:
            metrics, offset = semantics.empty_metrics, 0
        else:
            metrics, offset = checkpoint.rebase(opened, semantics), checkpoint.offset
        for message, _ in opened.public_records(offset):
            semantics.observe(message, metrics)
        projected = DisplayCheckpoint(source, source.size,
                     AppendCheckpoint.fingerprint(stream, source.size),
                     semantics, *metrics)
        opened.remember_append_checkpoint(self, projected)
        if checkpoint is None or checkpoint.semantics != semantics or offset != source.size:
            with suppress(OSError):
                self.write(projected)
        return projected
