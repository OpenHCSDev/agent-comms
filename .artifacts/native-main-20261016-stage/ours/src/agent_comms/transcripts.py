"""Bounded transcript history and routing store ownership."""

from __future__ import annotations

import json
import logging
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ClassVar

from .channel_targets import is_channel_target
from .errors import RelationViolationError
from .field_codec import FieldCodec, projected
from .declared_family import DeclaredFamily
from .historical_views import HistorySource
from .thread_identity import ThreadProvenance
from .message_bus import MessageBus
from .messages import Message
from .message_reference import MessageReference
from .messaging import Messaging
from .native_runtime_input import PublishedReplyRevision
from .external_transcript import ExternalDraftRow, ExternalTranscript
from .native_transcript import NativeTranscript, RecordsCut
from .private_path import FileRevision
from .bus_publication import CommittedDelivery
from .registration import Registration
from .routing import TurnRouting
from .threads import Thread
from .turn_lease import TurnLeaseFence
from .transcript_events import NoticeTranscript, TranscriptEvent, UserTranscript
from .transcript_routes import TranscriptRoutes, TranscriptRouteRevision
from .transcript_receipts import (
    AssignedSourceCursor, AssignedSourceIdentity, AssignedTranscriptSource,
    LiveAssignedTranscriptSource, TranscriptSource,
)
from .coordination_errors import StaleRevision, TranscriptChanged

if TYPE_CHECKING:
    from .wire_log import WireLog

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptCursor:
    session_file: str
    offset: int
    receipts: AssignedSourceCursor | None = None

    @property
    def source_identity(self) -> AssignedSourceIdentity | None:
        return self.receipts.identity if self.receipts is not None else None

    @property
    def wire_seq(self) -> int:
        return self.receipts.sequence if self.receipts is not None else 0

    def at_offset(self, offset: int) -> TranscriptCursor:
        from dataclasses import replace

        return replace(self, offset=offset)

    def at_sequence(self, sequence: int) -> TranscriptCursor:
        from dataclasses import replace

        if self.receipts is None:
            if sequence == 0:
                return self
            raise ValueError("Native-only cursor cannot advance an assigned wire source")
        return replace(self, receipts=self.receipts.at(sequence))

    def contains(self, other: TranscriptCursor) -> bool:
        return other.native_within(self) and other.receipts_within(self)

    def native_within(self, parent: TranscriptCursor) -> bool:
        return self.session_file == parent.session_file and self.offset <= parent.offset

    def receipts_within(self, parent: TranscriptCursor) -> bool:
        return self.receipts is None or parent.includes_receipts(self.receipts)

    def includes_receipts(self, receipts: AssignedSourceCursor) -> bool:
        return self.receipts is not None and self.receipts.contains(receipts)

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        if self.receipts is not None:
            self.receipts.require_source(identity)

    def require_native(self) -> None:
        if self.receipts is not None:
            raise ValueError("Recorded items cannot acquire an assigned wire cursor")

    def covers_incoming(self, sequence: int) -> bool:
        # Callers hold a canonical recipient assignment, never an arbitrary seq.
        return self.receipts is not None and self.receipts.covers(sequence)


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    events: tuple[TranscriptEvent, ...]
    before: TranscriptCursor
    after: TranscriptCursor
    has_older: bool
    has_newer: bool

    def resolve_sent_messages(self, reports):
        """Prove bounded full reports without replacing their input objects."""
        reports = tuple(reports)
        scope = self.after.source_identity
        resolved = (AssignedTranscriptSource.open(scope).resolve_sent(reports)
                    if scope is not None else reports)
        return dict(zip(map(id, reports), resolved))

    def metadata(self) -> dict[str, object]:
        return {
            field.name: FieldCodec.encode(getattr(self, field.name))
            for field in fields(self)
            if field.name != "events"
        }


@dataclass(frozen=True, slots=True)
class PageSource:
    """The retained stores one thread's transcript page is read from."""

    thread: Thread
    session_file: str
    inherited: bool
    root: Path
    log: WireLog
    routes: TranscriptRoutes
    records: NativeTranscript | ExternalTranscript
    identity: Callable[..., TranscriptReadIdentity]
    projection: TranscriptSource


@dataclass(frozen=True)
class TranscriptReadIdentity(DeclaredFamily, affix="TranscriptReadIdentity"):
    """All canonical inputs to a bounded native page, including annotations."""

    root: str
    requested_name: str
    thread: ThreadProvenance = field(metadata={"content_exclude": True})
    session_file: str
    records_cut: RecordsCut = field(metadata={"content_exclude": True})
    route_revision: TranscriptRouteRevision
    bus_revision: FileRevision | None = field(metadata={"content_exclude": True})
    coordination_revision: FileRevision | None = field(
        metadata={"content_exclude": True}
    )
    coordination_journal_revision: FileRevision | None = field(
        metadata={"content_exclude": True}
    )
    reply_revision: PublishedReplyRevision
    read_revision: FileRevision | None = field(metadata={"content_exclude": True})
    receipt_frontier: AssignedSourceCursor | None
    before: TranscriptCursor | None
    after: TranscriptCursor | None
    through: TranscriptCursor | None

    @projected(view="content", name="thread")
    def content_thread(self):
        return self.thread.incarnation

    @property
    def historical_source(self) -> str | None:
        return None

    @classmethod
    @abstractmethod
    def resolve_source(cls, owner, name, source_file, historical_source) -> PageSource:
        """Acquire this member's namespace, stores and identity constructor."""

    @abstractmethod
    def namespace(self, owner):
        """Reacquire this exact source without substituting a live namespace."""

    def publish_turn(self, publish: Callable[[str, Thread], None]) -> None:
        """Recorded evidence has no turn settlement or admission authority."""

    def recapture(self, owner) -> TranscriptReadIdentity:
        return owner.recapture_page_identity(self)

    def require_binding(self, owner, name) -> None:
        registry = self.namespace(owner)
        requested = registry.threads.get(registry.canonical_name(name))
        if requested is None:
            raise StaleRevision("Transcript request has no current source")
        if self.thread.incarnation.resolved(registry) != requested.incarnation:
            raise StaleRevision("Transcript witness belongs to another original thread")

    @projected(view="content", name="bus")
    def content_bus(self):
        # The certified frozen membership frontier owns relevant appends.
        # Keep inode custody: replacing the source is never an unrelated append.
        return self.bus_revision.identity if self.bus_revision is not None else None

    @property
    def content_identity(self) -> bytes:
        """Hashable source-owned inputs for existing preparation resources."""
        return json.dumps(
            FieldCodec.project(self, "content"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def page_bound(self) -> TranscriptCursor:
        """The original witnessed byte/receipt bound owns page preparation."""
        return self.through or TranscriptCursor(
            self.session_file,
            self.records_cut.offset,
            self.receipt_frontier,
        )

    def contains_records_cut(self, other: TranscriptReadIdentity) -> bool:
        """Whether ``other`` still contains the thread records this read captured."""
        return self.records_cut.holds(other.records_cut, self.page_bound.offset)

    def same_content(self, other: TranscriptReadIdentity) -> bool:
        """Fence the captured cut and its original publication proof.

        Reader acknowledgements and thread activity do not change page content.
        The scoped published relation remains fenced: an obligation can commit
        just after its wire append and replace the native final reply projection.
        Native growth after the captured byte bound belongs to a later update.
        """
        return self.contains_records_cut(other) and self.content_identity == other.content_identity

    def same_snapshot(self, other: TranscriptReadIdentity) -> bool:
        """Finished history and its source projection agree, including the latest record."""
        return self.records_cut == other.records_cut and self.same_content(other)

    def require_draft_snapshot(self, current: TranscriptReadIdentity) -> None:
        """The record source owns whether growth invalidates its current draft."""
        if not self.records_cut.draft_current(current.records_cut) or not self.same_content(current):
            raise StaleRevision("Transcript changed while reading its current draft")


@dataclass(frozen=True, slots=True)
class LiveTranscriptReadIdentity(TranscriptReadIdentity):
    thread: Thread = field(metadata={"content_exclude": True})

    @projected(view="content", name="thread")
    def content_thread(self):
        return self.thread.incarnation, self.thread.parent, self.thread.task

    @classmethod
    def resolve_source(cls, owner, name, source_file, historical_source):
        registry = owner.registry.snapshot()
        thread, session_file, inherited = owner._thread_transcript_source(
            name, source_file, registry=registry,
        )
        records = (NativeTranscript(Path(session_file)) if session_file else
                   ExternalTranscript(owner.root, thread.incarnation))
        return PageSource(thread, session_file, inherited,
                          owner.root, owner.bus.log, owner.routes, records, cls,
                          LiveAssignedTranscriptSource.for_thread(owner.root, thread, owner.bus.log))

    def namespace(self, owner):
        return owner.registry.snapshot()

    def publish_turn(self, publish: Callable[[str, Thread], None]) -> None:
        publish(self.root, self.thread)


@dataclass(frozen=True, slots=True)
class RecordedTranscriptReadIdentity(TranscriptReadIdentity):
    source: HistorySource = field(kw_only=True, metadata={"content_exclude": True})

    @property
    def historical_source(self) -> str:
        return self.source.key

    @projected(view="content", name="source")
    def content_source(self):
        return (self.source.key, self.source.original_root, self.source.wire_root_id,
                self.source.bus_identity, self.source.size,
                self.source.snapshot_bus_revision, self.source.snapshot_registry_revision)

    @classmethod
    def resolve_source(cls, owner, name, source_file, historical_source):
        from .wire_log import WireLog

        source = HistorySource.require(owner.bus.history.sources(), historical_source)
        thread = source.provenance.require(name)
        session_file = thread.session_file or ""
        if source_file is not None and source_file != session_file:
            raise TranscriptChanged()
        root = Path(source.root)
        log = WireLog(root / "bus.jsonl")
        records = (NativeTranscript(Path(session_file)) if session_file else
                   ExternalTranscript(root, thread.incarnation))
        return PageSource(thread, session_file, False, root,
                          log, TranscriptRoutes(root), records,
                          partial(cls, source=source),
                          AssignedTranscriptSource.for_recorded(root, thread, log, records))

    def namespace(self, owner):
        current = HistorySource.require(owner.bus.history.sources(), self.source.key)
        if current != self.source:
            raise StaleRevision("Transcript witness belongs to another recorded source")
        return current.provenance

    def recapture(self, owner) -> TranscriptReadIdentity:
        self.namespace(owner)
        return TranscriptReadIdentity.recapture(self, owner)


@dataclass(frozen=True, slots=True)
class TranscriptRead:
    """Deferred canonical read retaining its resolved source and captured identity."""

    owner: Transcripts
    identity: TranscriptReadIdentity
    source: PageSource
    # Required viewer reads stay pending in the read lifecycle. Their SQL
    # attempt must not consume that wait by blocking a presentation worker.
    source_wait: ClassVar[float] = 0

    @classmethod
    @contextmanager
    def publication_source(cls, root):
        from .coordination_database import CoordinationStore
        from .coordinated_runtime_schema import assert_native_runtime_schema
        from .coordination_response import _assert_response_schema
        from .recovery_projection import _preflight

        database = root / "coordination.sqlite3"
        failure = _preflight(database)
        if failure == "missing":
            yield None
            return
        if failure:
            raise RelationViolationError("Native reply source has an invalid coordinator")
        with CoordinationStore.observing(database) as db:
            assert_native_runtime_schema(db)
            _assert_response_schema(db)
            yield db

    def current(self) -> bool:
        return self.identity == self.current_identity()

    def content_current(self) -> bool:
        return self.identity.same_content(self.current_identity())

    def current_identity(self) -> TranscriptReadIdentity:
        return self.identity.recapture(self.owner)

    def _read_page(self) -> tuple[TranscriptPage, ExternalDraftRow | None]:
        identity = self.identity
        return self.owner._thread_transcript_page(
            self.source,
            before=identity.before,
            after=identity.after,
            max_messages=20,
            max_bytes=64 * 1024,
            through=identity.page_bound,
        )

    def read(self) -> TranscriptPage:
        page, _ = self._read_page()
        if not self.content_current():
            raise StaleRevision("Transcript read inputs changed during preparation")
        return page

    def read_snapshot(self) -> tuple[TranscriptPage, ExternalDraftRow | None]:
        """Read the page and external draft from one retained source lifecycle."""
        page, draft = self._read_page()
        self.identity.require_draft_snapshot(self.current_identity())
        return page, draft

    def current_draft(self) -> ExternalDraftRow | None:
        """Read current text beside this page, before its finished cut changes."""
        with self.source.records.reading() as records:
            draft = records.current_draft()
        self.identity.require_draft_snapshot(self.current_identity())
        return draft


@dataclass(frozen=True, slots=True)
class TranscriptSourceWindow:
    """One transcript read and its same-cut conversation source windows."""

    read: TranscriptRead
    sources: tuple[CommittedDelivery, ...]
    sent_sources: tuple[CommittedDelivery, ...]


class Transcripts:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, messaging: Messaging):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.messaging = messaging
        self.routes = TranscriptRoutes(root)
        self.page_reads = 0

    def bind_page_read(
        self,
        name: str,
        identity: TranscriptReadIdentity,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        through: TranscriptCursor | None = None,
    ) -> TranscriptRead:
        """Bind the original published witness to this actual source request."""
        if identity.root != str(self.root):
            raise StaleRevision("Transcript witness belongs to another root")
        if (identity.before, identity.after, identity.through) != (before, after, through):
            raise StaleRevision("Transcript witness belongs to another page window")
        identity.require_binding(self, name)
        source = self._page_source(name, identity.page_bound, identity.historical_source)
        return TranscriptRead(self, identity, source)

    def capture_page_read(
        self,
        name: str,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        through: TranscriptCursor | None = None,
        historical_source: str | None = None,
    ) -> TranscriptRead:
        page = self._page_source(name, through, historical_source)
        frontier = page.projection.frontier
        identity = self._capture_page_identity(
            page, name, frontier, before=before, after=after, through=through,
        )
        return TranscriptRead(self, identity, page)

    def capture_page_window(
        self,
        name: str,
        *,
        source_limit: int = 1,
        sent_source_limit: int = 0,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        through: TranscriptCursor | None = None,
        historical_source: str | None = None,
    ) -> TranscriptSourceWindow:
        """Expose the source owner's complete identity without parsing its page.

        Native bytes alone are insufficient: input display/routing and older
        bus receipts contribute to the same projection. Registry-owned thread
        metadata also carries inherited fork text and source selection.
        """
        from .presentation import MessageNotification

        if not 1 <= source_limit <= MessageNotification.window_limit:
            raise ValueError("Transcript source capture requires a bounded window")
        if not 0 <= sent_source_limit <= MessageNotification.window_limit:
            raise ValueError("Sent source capture requires a bounded window")
        page = self._page_source(name, through, historical_source)
        captured = page.projection.window(limit=source_limit, sent_limit=sent_source_limit)
        identity = self._capture_page_identity(
            page, name, captured.cursor, before=before, after=after, through=through,
        )
        return TranscriptSourceWindow(
            TranscriptRead(self, identity, page), captured.sources, captured.sent_sources
        )

    def recapture_page_identity(
        self, identity: TranscriptReadIdentity,
    ) -> TranscriptReadIdentity:
        """Recapture identity facts without reconstructing presentation source windows."""
        page = self._page_source(
            identity.requested_name, identity.through, identity.historical_source
        )
        frontier = page.projection.frontier
        return self._capture_page_identity(
            page,
            identity.requested_name,
            frontier,
            before=identity.before,
            after=identity.after,
            through=identity.through,
        )

    def _capture_page_identity(
        self,
        page: PageSource,
        name: str,
        receipt_frontier: AssignedSourceCursor | None,
        *,
        before: TranscriptCursor | None,
        after: TranscriptCursor | None,
        through: TranscriptCursor | None,
    ) -> TranscriptReadIdentity:
        root, thread, records = page.root, page.thread, page.records
        with records.reading() as captured_records:
            records_cut = captured_records.cut()
        return page.identity(
            str(self.root),
            name,
            thread,
            page.session_file,
            records_cut,
            page.routes.revision(),
            FileRevision.capture(page.log.path),
            FileRevision.capture(root / "coordination.sqlite3"),
            FileRevision.capture(root / "coordination.sqlite3-wal"),
            page.projection.reply_revision(records),
            FileRevision.capture(root / self.bus.reads.path.name),
            receipt_frontier,
            before,
            after,
            through,
        )

    def external(self, name: str) -> ExternalTranscript:
        """Where a CLI bridge publishes the items of its sessionless bus thread."""
        page = self._page_source(name, None, None)
        if page.session_file:
            raise ValueError(f"@{name} reads a native session; a CLI cannot publish its transcript")
        return page.records

    def thread_draft(self, name: str) -> ExternalDraftRow | None:
        """The canonical source's current output, separate from paged history."""
        with self._page_source(name, None, None).records.reading() as records:
            return records.current_draft()

    def _page_source(self, name, through, historical_source):
        member = (LiveTranscriptReadIdentity if historical_source is None
                  else RecordedTranscriptReadIdentity)
        return member.resolve_source(
            self, name, through.session_file if through is not None else None, historical_source,
        )

    def thread_transcript(
        self, name: str, *, max_messages: int = 20, max_bytes: int = 64 * 1024
    ) -> Sequence[TranscriptEvent]:
        """The same canonical source supplies bounded preview and paged history."""
        if max_messages <= 0 or max_bytes <= 0:
            return ()
        page = self.thread_transcript_page(name, max_messages=max_messages, max_bytes=max_bytes)
        return (
            (NoticeTranscript("Earlier transcript content was omitted from this bounded view."),)
            if page.has_older
            else ()
        ) + page.events

    def _thread_transcript_source(
        self, name: str, source_file: str | None = None, *, registry
    ) -> tuple[Thread, str, bool]:
        """Resolve a temporary inherited view without changing runtime ownership."""
        thread = registry.require(name)
        if thread.session_file and (source_file is None or source_file == thread.session_file):
            return thread, thread.session_file, False
        ancestor = thread
        visited = {thread.name}
        while ancestor.parent is not None:
            ancestor = registry.require(ancestor.parent)
            if ancestor.name in visited:
                break
            visited.add(ancestor.name)
            if ancestor.session_file and (
                source_file is None or source_file == ancestor.session_file
            ):
                return thread, ancestor.session_file, True
        if source_file:
            raise TranscriptChanged()
        return thread, "", bool(thread.parent)

    @staticmethod
    def _fork_start_events(thread: Thread) -> tuple[TranscriptEvent, ...]:
        if not thread.parent:
            return ()
        return (
            NoticeTranscript(f"Forked from @{thread.parent}. This thread started with:"),
            *((UserTranscript(thread.task),) if thread.task else ()),
        )

    def thread_transcript_page(
        self,
        name: str,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        max_messages: int = 20,
        max_bytes: int = 64 * 1024,
        through: TranscriptCursor | None = None,
        historical_source: str | None = None,
    ) -> TranscriptPage:
        """Read one adjacent page with exclusive, file-bound byte cursors."""
        if before is not None and after is not None:
            raise ValueError("Choose one transcript paging direction.")
        if max_messages <= 0 or max_bytes <= 0:
            raise ValueError("Transcript page budgets must be positive.")
        # A mounted inherited window remains pinned to its ancestor and byte
        # boundary when the child persists its own session. New unpinned reads
        # select the child's file; existing scroll cursors keep working.
        page = self._page_source(name, through, historical_source)
        return self._thread_transcript_page(
            page,
            before=before,
            after=after,
            max_messages=max_messages,
            max_bytes=max_bytes,
            through=through,
        )[0]

    def _thread_transcript_page(
        self,
        page: PageSource,
        *,
        before: TranscriptCursor | None,
        after: TranscriptCursor | None,
        max_messages: int,
        max_bytes: int,
        through: TranscriptCursor | None,
    ) -> tuple[TranscriptPage, ExternalDraftRow | None]:
        """Read page and draft while their record owner retains one source lifecycle."""
        self.page_reads += 1
        from .transcript_receipts import (
            EarlierTranscript,
            LaterTranscript,
        )

        thread, session_file = page.thread, page.session_file
        cursor = before or after
        if cursor and (cursor.session_file != session_file or cursor.offset < 0):
            raise TranscriptChanged()
        receipts = page.projection
        with page.records.reading() as reader:
            size = reader.cut().offset
            if cursor and cursor.offset > size:
                raise TranscriptChanged()
            if through is not None:
                if through.session_file != session_file or not 0 <= through.offset <= size:
                    raise TranscriptChanged()
                size = through.offset
                if cursor and cursor.offset > size:
                    raise ValueError("Cursor is outside the transcript window.")
            frontier = through or TranscriptCursor(session_file, size, receipts.frontier)
            receipts.require_cursor(frontier)
            if frontier.wire_seq < 0 or (cursor is not None and not frontier.contains(cursor)):
                raise ValueError("Cursor is outside the combined transcript source")
            traversal = LaterTranscript() if after is not None else EarlierTranscript()
            initial = cursor or frontier
            with reader.projection(receipts, page.routes) as project:
                consumed, records = traversal.read_records(
                    reader, project, receipts, initial, frontier,
                    max_messages=max_messages, max_bytes=max_bytes,
                )
            draft = reader.current_draft()
        start, end = traversal.bounds(initial, consumed)
        events = traversal.finish(records)
        if page.inherited and not after and (cursor is None or cursor == frontier):
            events = (*events, *self._fork_start_events(thread))
        return (
            TranscriptPage(
                events, start, end,
                start.offset > 0 or start.wire_seq > 0,
                end != frontier,
            ),
            draft,
        )

    def transcript_checkpoint(self, name: str) -> TranscriptCursor:
        from .transcript_receipts import AssignedTranscriptSource

        registry = self.registry.snapshot()
        thread = registry.require(name)
        session_file = thread.session_file or ""
        path = Path(session_file)
        return TranscriptCursor(
            session_file,
            path.stat().st_size if session_file and path.is_file() else 0,
            AssignedTranscriptSource.for_thread(self.root, thread, self.bus.log).frontier,
        )

    def record_turn_publication(
        self, *, lease: TurnLeaseFence, checkpoint: TranscriptCursor,
        routing: TurnRouting, published: tuple[MessageReference, ...],
    ) -> None:
        """Publish original native/wire relations before this exact turn retires."""
        self.routes.record_turn_publication(
            self.registry, self.bus.log, lease, checkpoint, routing, published
        )

