"""Transcript items a CLI harness publishes for its sessionless bus thread.

A thread without a native session has no session file of its own; the CLI
bridge publishes each finished item (a reply, a reasoning summary, a tool call,
a compaction) here once. Finished items are append-only, so the thread's page
merges them with its bus messages exactly as a native session's records are
merged: the item's sequence is its position. What the CLI is doing before an
item finishes is the thread's activity, reported through Core's activity log.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .bus_publication import stable_thread_lookup
from .native_runtime_input import PublishedReplyRevision
from .native_transcript import RecordsCut
from .thread_identity import ThreadIncarnation
from .transcript_events import TranscriptEvent
from .typed_table import Column, TypedStore, TypedTable


@dataclass(frozen=True)
class ExternalItemRow(TypedTable):
    """One finished CLI item, at the position it took when it finished."""

    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str = field(metadata={"sql": Column(primary_key=True)})
    item_id: str = field(metadata={"sql": Column(primary_key=True)})
    seq: int
    events: tuple[TranscriptEvent, ...]
    unique = (("thread", "seq"),)


@dataclass(frozen=True)
class ExternalRecord:
    """An item at its page position, in the shape the page merge consumes."""

    seq: int
    events: tuple[TranscriptEvent, ...]

    @property
    def start(self) -> int:
        return self.seq - 1

    @property
    def end(self) -> int:
        return self.seq

    @property
    def size(self) -> int:
        return sum(event.text_size for event in self.events)

    def incomplete_tail(self, through: int) -> bool:
        return False  # Only finished items are records.


@dataclass(frozen=True)
class ExternalCut(RecordsCut):
    """The thread's newest finished item sequence when read."""

    offset: int

    def contains(self, current: ExternalCut, through: int) -> bool:
        # Finished items are append-only: later items belong to the next update.
        return current.offset >= self.offset


@dataclass(frozen=True, slots=True)
class ExternalTranscript(TypedStore):
    """One sessionless thread's CLI items: published by its bridge, paged by Core."""

    root: Path
    incarnation: ThreadIncarnation
    filename: ClassVar[str] = "external_transcripts.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (ExternalItemRow,)
    # The CLI keeps its own items; a lost tail is republished when a watch starts.
    synchronous: ClassVar[str] = "NORMAL"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        database = root / cls.filename
        return database, database.with_name(database.name + "-wal")

    @property
    def lookup(self) -> str:
        return stable_thread_lookup(self.incarnation.created_at)

    def finish(self, *, turn_id: str, item_id: str,
               events: tuple[TranscriptEvent, ...]) -> None:
        """Append a finished item once; an item with no presentation adds nothing."""
        if not events:
            return
        key = dict(thread=self.lookup, turn_id=turn_id, item_id=item_id)
        with self.transaction(write=True) as connection:
            if ExternalItemRow.one(connection, **key) is None:
                ExternalItemRow(seq=self._cut(connection).offset + 1, events=events,
                                **key).insert(connection)

    def _cut(self, connection) -> ExternalCut:
        return ExternalCut(*connection.execute(
            f'SELECT coalesce(max(seq), 0) FROM "{ExternalItemRow.declared_name}" '
            'WHERE thread = ?', (self.lookup,)).fetchone())

    def cut(self) -> ExternalCut:
        if not self.path.exists():
            return ExternalCut(0)  # No CLI has published an item.
        with self.transaction(write=False) as connection:
            return self._cut(connection)

    def reply_revision(self, root: Path, thread) -> PublishedReplyRevision:
        return PublishedReplyRevision(0, 0)  # Core publishes no native replies here.

    def _records(self, where: str, parameters: tuple, *,
                 descending: bool) -> Iterator[ExternalRecord]:
        if not self.path.exists():
            return
        with self.transaction(write=False) as connection:
            rows = ExternalItemRow.stream(
                connection, where=where, parameters=parameters, order_by=("seq",),
                descending=descending, thread=self.lookup)
            try:
                for row in rows:
                    yield ExternalRecord(row.seq, row.events)
            finally:
                rows.close()

    def forward(self, after: int, through: int) -> Iterator[ExternalRecord]:
        return self._records("seq > ? AND seq <= ?", (after, through), descending=False)

    def reverse(self, before: int) -> Iterator[ExternalRecord]:
        return self._records("seq <= ?", (before,), descending=True)

    @contextmanager
    def projection(self, receipts, routes):
        def project(records, *, max_records, max_bytes):
            return ((record, record.events) for record in records)

        yield project

