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
from sqlite3 import Connection
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .native_runtime_input import PublishedReplyRevision
from .native_transcript import RecordsCut
from .thread_identity import ThreadIncarnation
from .transcript_events import TranscriptEvent
from .transcript_receipts import TranscriptSource
from .typed_table import Column, TypedRow, TypedStore, TypedTable


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
class ExternalDraftRow(TypedTable):
    """The current CLI item, replaced as it grows and retired at completion."""

    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]


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
class ExternalCut(RecordsCut, TypedRow):
    """The thread's newest finished item sequence when read."""

    offset: int

    def contains(self, current: ExternalCut, through: int) -> bool:
        # Finished items are append-only: later items belong to the next update.
        return current.offset >= self.offset

    def draft_current(self, current: RecordsCut) -> bool:
        # Finishing a CLI item retires its draft atomically. Its current draft
        # must not be paired with a page captured before that completion.
        return self == current


@dataclass(frozen=True, slots=True)
class ExternalTranscriptRead:
    """One SQLite snapshot of a sessionless transcript's finished and draft rows."""

    transcript: ExternalTranscript
    connection: Connection | None

    @property
    def lookup(self) -> str:
        return self.transcript.lookup

    def current_draft(self) -> ExternalDraftRow | None:
        if self.connection is None:
            return None
        return ExternalDraftRow.one(self.connection, thread=self.lookup)

    def cut(self) -> ExternalCut:
        if self.connection is None:
            return ExternalCut(0)
        return self.transcript._cut(self.connection)

    def _records(self, where: str, parameters: tuple, *,
                 descending: bool) -> Iterator[ExternalRecord]:
        if self.connection is None:
            return
        rows = ExternalItemRow.stream(
            self.connection, where=where, parameters=parameters, order_by=("seq",),
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


@dataclass(frozen=True, slots=True)
class ExternalTranscript(TranscriptSource, TypedStore):
    """One sessionless thread's CLI items: published by its bridge, paged by Core."""

    root: Path
    incarnation: ThreadIncarnation
    filename: ClassVar[str] = "external_transcripts.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (ExternalItemRow, ExternalDraftRow)
    # The CLI keeps its own items; a lost tail is republished when a watch starts.
    synchronous: ClassVar[str] = "NORMAL"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        database = root / cls.filename
        return database, database.with_name(database.name + "-wal")

    @property
    def lookup(self) -> str:
        return self.incarnation.lookup

    def finish(self, *, turn_id: str, item_id: str,
               events: tuple[TranscriptEvent, ...]) -> None:
        """Append a finished item once; an item with no presentation adds nothing."""
        key = dict(thread=self.lookup, turn_id=turn_id, item_id=item_id)
        with self.writing() as connection:
            if events and ExternalItemRow.one(connection, **key) is None:
                ExternalItemRow(seq=self._cut(connection).offset + 1, events=events,
                                **key).insert(connection)
            connection.execute(
                f'DELETE FROM "{ExternalDraftRow.declared_name}" '
                'WHERE thread = ? AND turn_id = ? AND item_id = ?',
                (self.lookup, turn_id, item_id))

    def draft(self, *, turn_id: str, item_id: str,
              events: tuple[TranscriptEvent, ...]) -> None:
        with self.writing() as connection:
            ExternalDraftRow(self.lookup, turn_id, item_id, events).upsert(connection)

    def end_turn(self) -> None:
        if not self.path.exists():
            return
        with self.writing() as connection:
            connection.execute(f'DELETE FROM "{ExternalDraftRow.declared_name}" WHERE thread = ?',
                               (self.lookup,))

    @contextmanager
    def reading(self):
        """Retain one read transaction across cut, page records, and draft."""
        if not self.path.exists():
            yield ExternalTranscriptRead(self, None)
            return
        with self.snapshot() as connection:
            yield ExternalTranscriptRead(self, connection)

    def _cut(self, connection) -> ExternalCut:
        return ExternalCut.read(connection.execute(
            f'SELECT coalesce(max(seq), 0) AS offset FROM "{ExternalItemRow.declared_name}" '
            'WHERE thread = ?', (self.lookup,)))[0]

    def current_draft(self) -> ExternalDraftRow | None:
        with self.reading() as records:
            return records.current_draft()

    def cut(self) -> ExternalCut:
        with self.reading() as records:
            return records.cut()

    def publication_revision(self, root: Path, thread) -> PublishedReplyRevision:
        return PublishedReplyRevision(0, 0)  # Core publishes no native replies here.

