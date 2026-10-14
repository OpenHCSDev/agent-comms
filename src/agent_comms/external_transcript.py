"""Transcript items a CLI harness publishes for its sessionless bus thread.

A thread without a native session has no session file of its own; the CLI
bridge publishes each of its items (a reply, a reasoning summary, a tool call,
a compaction) here, whole, every time the item changes. The thread's page
merges these items with its bus messages exactly as a native session's
records are merged: the item's sequence is its position.
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
from .typed_table import Column, Index, TypedStore, TypedTable


@dataclass(frozen=True)
class ExternalItemRow(TypedTable):
    """One CLI item's current presentation; its position is fixed at first publication."""

    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str = field(metadata={"sql": Column(primary_key=True)})
    item_id: str = field(metadata={"sql": Column(primary_key=True)})
    seq: int
    # Every publication to a thread takes that thread's next revision.
    revision: int
    events: tuple[TranscriptEvent, ...]
    unique = (("thread", "seq"),)
    indexes = (Index(("thread", "revision")),)


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
        return False  # Each publication is the whole item.


@dataclass(frozen=True)
class ExternalCut(RecordsCut):
    """The thread's newest item sequence and publication revision when read."""

    offset: int
    revision: int

    def contains(self, current: ExternalCut, through: int) -> bool:
        # Items change in place, so any publication can change captured content.
        return current == self


@dataclass(frozen=True, slots=True)
class ExternalTranscript(TypedStore):
    """One sessionless thread's CLI items: published by its bridge, paged by Core."""

    root: Path
    incarnation: ThreadIncarnation
    filename: ClassVar[str] = "external_transcripts.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (ExternalItemRow,)
    # Items are republished on every delta; a lost tail is replaced by the next.
    synchronous: ClassVar[str] = "NORMAL"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        database = root / cls.filename
        return database, database.with_name(database.name + "-wal")

    @property
    def lookup(self) -> str:
        return stable_thread_lookup(self.incarnation.created_at)

    def publish(self, *, turn_id: str, item_id: str,
                events: tuple[TranscriptEvent, ...]) -> None:
        """Replace this item's presentation, keeping its first position."""
        with self.transaction(write=True) as connection:
            current = ExternalItemRow.one(
                connection, thread=self.lookup, turn_id=turn_id, item_id=item_id)
            cut = self._cut(connection)
            ExternalItemRow(
                self.lookup, turn_id, item_id,
                current.seq if current is not None else cut.offset + 1,
                cut.revision + 1, events,
            ).upsert(connection)

    def _cut(self, connection) -> ExternalCut:
        return ExternalCut(*connection.execute(
            'SELECT coalesce(max(seq), 0), coalesce(max(revision), 0) '
            f'FROM "{ExternalItemRow.declared_name}" WHERE thread = ?', (self.lookup,)).fetchone())

    def cut(self) -> ExternalCut:
        if not self.path.exists():
            return ExternalCut(0, 0)  # No CLI has published an item.
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

