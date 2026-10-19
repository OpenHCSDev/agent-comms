"""Bounded transcript history and routing store ownership."""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Callable, ClassVar

from .agent_backend import RuntimeLocation
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
from .external_transcript import ExternalTranscript
from .native_transcript import NativeTranscript, RecordsCut
from .private_path import FileRevision
from .bus_publication import CommittedDelivery
from .registration import Registration
from .routing import TurnRouting
from .threads import Thread
from .turn_lease import TurnLeaseFence
from .turn_digests import ThreadTurns, TurnBound, TurnDigestRead
from .transcript_events import ForkOriginTranscript, ForkTaskTranscript, TranscriptEvent
from .transcript_routes import TranscriptRoutes, TranscriptRouteRevision
from .transcript_sources import (
    AssignedSourceCursor, AssignedTranscriptSource, EarlierTranscript, NoWireFrontier,
    LaterTranscript, LiveAssignedTranscriptSource, TranscriptDraft, TranscriptSource, WireFrontier,
)
from .coordination_errors import CoordinationReadUnavailable, StaleRevision, TranscriptChanged
from .coordination_database import CoordinationStore
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_response import _assert_response_schema
from .presentation import MessageNotification
from .wire_log import WireLog

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptCursor(DeclaredFamily, affix="TranscriptCursor"):
    """A native transcript position; WireTranscriptCursor adds an assigned wire source."""

    session_file: ClassVar[str | None] = None
    offset: ClassVar[int] = 0
    wire: ClassVar[WireFrontier] = NoWireFrontier()

    def at_offset(self, offset: int) -> TranscriptCursor:
        return replace(self, offset=offset)

    def at_sequence(self, sequence: int) -> TranscriptCursor:
        return self.with_frontier(self.wire.at(sequence))

    @abstractmethod
    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor: ...

    @abstractmethod
    def mark_read(self, reads, viewer, runtime: RuntimeLocation) -> None: ...

    def contains(self, other: TranscriptCursor) -> bool:
        return (self.session_file == other.session_file and other.offset <= self.offset
                and other.wire.within(self.wire))

    def covers_incoming(self, sequence: int) -> bool:
        # Callers hold a canonical recipient assignment, never an arbitrary seq.
        return 0 < sequence <= self.wire.sequence

    def over_native(self, prefixes: Sequence[TranscriptCursor]) -> TranscriptCursor:
        """This wire position over the furthest committed native prefix within it: wire
        assignment can advance without transferring anonymous native output."""
        within = tuple(prefix for prefix in prefixes if prefix.wire.within(self.wire))
        prefix = max((prefix for prefix in within if prefix.session_file == self.session_file),
                     key=lambda prefix: prefix.offset, default=self.inherited_prefix(within))
        return self.with_prefix(prefix)

    def inherited_prefix(self, prefixes: Sequence[TranscriptCursor]) -> TranscriptCursor:
        return self.at_offset(0)

    def with_prefix(self, prefix: TranscriptCursor) -> TranscriptCursor:
        return self.at_offset(prefix.offset)


@dataclass(frozen=True, slots=True)
class NoNativeTranscriptCursor(TranscriptCursor):
    """A thread owns no native read mark, even while showing an inherited source."""

    source: RuntimeLocation = field(kw_only=True)
    offset: int = 0
    wire: WireFrontier = field(default_factory=NoWireFrontier)

    @property
    def session_file(self):
        return self.source.read_key

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return replace(self, wire=frontier)

    def mark_read(self, reads, viewer, runtime: RuntimeLocation) -> None:
        """There is no painted native transcript to acknowledge."""


@dataclass(frozen=True, slots=True)
class NativeTranscriptCursor(TranscriptCursor):
    session_file: str = field()
    offset: int = field()

    def inherited_prefix(self, prefixes: Sequence[TranscriptCursor]) -> TranscriptCursor:
        return next(iter(prefixes), self.at_offset(0))

    def with_prefix(self, prefix: TranscriptCursor) -> TranscriptCursor:
        return prefix.with_frontier(self.wire)

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return frontier.cursor(self.session_file, self.offset)

    def mark_read(self, reads, viewer, runtime: RuntimeLocation) -> None:
        if self.session_file != runtime.read_key:
            raise TranscriptChanged()
        for key in runtime.read_keys():
            reads.mark_read(viewer, key, self.offset, NativeTranscript(Path(key)).cut())


@dataclass(frozen=True, slots=True)
class WireTranscriptCursor(NativeTranscriptCursor):
    wire: AssignedSourceCursor = field()


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    events: tuple[TranscriptEvent, ...]
    before: TranscriptCursor
    after: TranscriptCursor
    has_older: bool
    has_newer: bool
    # The read owns this witness; no page exists without its source snapshot.
    snapshot: TranscriptReadIdentity

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
    runtime: RuntimeLocation = field(metadata={"wire_name": "session_file"})
    inherited: bool
    root: Path
    log: WireLog
    routes: TranscriptRoutes
    records: TranscriptSource
    identity: Callable[..., TranscriptReadIdentity]
    projection: TranscriptSource


@dataclass(frozen=True)
class TranscriptReadIdentity(DeclaredFamily, affix="TranscriptReadIdentity"):
    """All canonical inputs to a bounded native page, including annotations."""

    root: str
    requested_name: str
    thread: ThreadProvenance = field(metadata={"content_exclude": True})
    runtime: RuntimeLocation
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
    wire_frontier: WireFrontier
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
    def resolve_source(cls, owner, name, source_runtime, historical_source) -> PageSource:
        """Acquire this member's namespace, stores and identity constructor."""

    @abstractmethod
    def namespace(self, owner):
        """Reacquire this exact source without substituting a live namespace."""

    def publish_turn(self, publish: Callable[[str, Thread], None]) -> None:
        """Recorded evidence cannot settle a turn or permit new input."""

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
        # Appends beyond this page's assigned messages do not change its content.
        # Replacing the bus file does.
        return self.bus_revision.identity if self.bus_revision is not None else None

    @property
    def page_bound(self) -> TranscriptCursor:
        """The last native byte and assigned message included in this page."""
        return self.through or self.thread.runtime.transcript_cursor(self.wire_frontier, self.records_cut.offset, source=self.runtime)

    def contains_records_cut(self, other: TranscriptReadIdentity) -> bool:
        """Whether ``other`` still contains the thread records this read captured."""
        return self.records_cut.holds(other.records_cut, self.page_bound.offset)

    def same_content(self, other: TranscriptReadIdentity) -> bool:
        """Compare the captured records and values that affect this page.

        Reading messages or updating activity does not change the content.
        A committed reply can replace its provisional native output.
        Records appended beyond this page belong to a later update.
        """
        return self.contains_records_cut(other) and (
            FieldCodec.project(self, "content") == FieldCodec.project(other, "content")
        )

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
    def resolve_source(cls, owner, name, source_runtime, historical_source):
        registry = owner.registry.snapshot()
        thread, runtime, inherited = owner._thread_transcript_source(
            name, source_runtime, registry=registry,
        )
        records = runtime.history_source(owner.root, thread.incarnation)
        return PageSource(thread, runtime, inherited,
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
    def resolve_source(cls, owner, name, source_runtime, historical_source):
        source = HistorySource.require(owner.bus.history.sources(), historical_source)
        thread = source.provenance.require(name)
        runtime = thread.runtime
        if source_runtime is not None and source_runtime != runtime.read_key:
            raise TranscriptChanged()
        root = Path(source.root)
        log = WireLog(root / "bus.jsonl")
        records = runtime.history_source(root, thread.incarnation)
        return PageSource(thread, runtime, False, root,
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
        with CoordinationStore.observed(root / "coordination.sqlite3").observing(cls) as db:
            yield db

    @classmethod
    def capture(cls, db):
        assert_native_runtime_schema(db)
        _assert_response_schema(db)
        return db

    @classmethod
    def missing_store(cls, store):
        return None

    @classmethod
    def invalid_store(cls, store):
        raise RelationViolationError("Native reply source has an invalid coordinator")

    def current(self) -> bool:
        return self.identity == self.current_identity()

    def content_current(self) -> bool:
        return self.identity.same_content(self.current_identity())

    def current_identity(self) -> TranscriptReadIdentity:
        return self.identity.recapture(self.owner)

    def _read_page(self) -> tuple[TranscriptPage, TranscriptDraft]:
        identity = self.identity
        return self.owner._thread_transcript_page(
            self.source,
            identity=identity,
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

    def read_snapshot(self) -> tuple[TranscriptPage, TranscriptDraft]:
        """Read the page and external draft from one retained source lifecycle."""
        page, draft = self._read_page()
        self.identity.require_draft_snapshot(self.current_identity())
        return page, draft

    def current_draft(self) -> TranscriptDraft:
        """Read current text beside this page, before its finished cut changes."""
        with self.source.records.reading() as records:
            draft = records.current_draft(self.source.root, self.source.thread.incarnation)
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
        bus evidences contribute to the same projection. Registry-owned thread
        metadata also carries inherited fork text and source selection.
        """
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
        wire_frontier: WireFrontier,
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
            page.runtime,
            records_cut,
            page.routes.revision(),
            FileRevision.capture(page.log.path),
            FileRevision.capture(root / "coordination.sqlite3"),
            FileRevision.capture(root / "coordination.sqlite3-wal"),
            page.projection.reply_revision(records),
            FileRevision.capture(root / self.bus.reads.path.name),
            wire_frontier,
            before,
            after,
            through,
        )

    def turns(self, name: str, bound: TurnBound) -> ThreadTurns:
        return TurnDigestRead(self.root, self.registry.snapshot().require(name), bound).read()

    def external(self, name: str) -> ExternalTranscript:
        """Where a CLI bridge publishes the items of its sessionless bus thread."""
        page = self._page_source(name, None, None)
        return page.runtime.external_records(page.root, page.thread.incarnation)

    def _page_source(self, name, through, historical_source):
        member = (LiveTranscriptReadIdentity if historical_source is None
                  else RecordedTranscriptReadIdentity)
        return member.resolve_source(
            self, name, through.session_file if through is not None else None, historical_source,
        )

    def _thread_transcript_source(
        self, name: str, source_runtime: str | None = None, *, registry
    ) -> tuple[Thread, RuntimeLocation, bool]:
        """Resolve a temporary inherited view without changing runtime ownership."""
        thread = registry.require(name)
        for location, inherited in thread.runtime.history_locations(thread, registry, set()):
            if source_runtime is None or source_runtime == location.read_key:
                return thread, location, inherited
        raise TranscriptChanged()

    @staticmethod
    def _fork_start_events(thread: Thread) -> tuple[TranscriptEvent, ...]:
        if not thread.parent:
            return ()
        return (
            ForkOriginTranscript(f"Forked from @{thread.parent}. This thread started with:"),
            *((ForkTaskTranscript(thread.task),) if thread.task else ()),
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
        read = self.capture_page_read(
            name, before=before, after=after, through=through,
            historical_source=historical_source,
        )
        page, _ = self._thread_transcript_page(
            read.source, identity=read.identity,
            before=before, after=after,
            max_messages=max_messages, max_bytes=max_bytes,
            through=read.identity.page_bound,
        )
        if not read.content_current():
            raise StaleRevision("Transcript read inputs changed during preparation")
        return page

    def _thread_transcript_page(
        self,
        page: PageSource,
        *,
        identity: TranscriptReadIdentity,
        before: TranscriptCursor | None,
        after: TranscriptCursor | None,
        max_messages: int,
        max_bytes: int,
        through: TranscriptCursor | None,
    ) -> tuple[TranscriptPage, TranscriptDraft]:
        """Read page and draft while their record owner retains one source lifecycle."""
        self.page_reads += 1
        thread, runtime = page.thread, page.runtime
        cursor = before or after
        if cursor and cursor.session_file != runtime.read_key:
            raise TranscriptChanged()
        source = page.projection
        with page.records.reading() as reader:
            size = reader.cut().offset
            if cursor and not reader.valid_offset(cursor.offset, size):
                raise TranscriptChanged()
            if through is not None:
                if through.session_file != runtime.read_key or not reader.valid_offset(through.offset, size):
                    raise TranscriptChanged()
                size = through.offset
                if cursor and not reader.valid_offset(cursor.offset, size):
                    raise TranscriptChanged()
            frontier = through or thread.runtime.transcript_cursor(source.frontier, size, source=runtime)
            source.require_cursor(frontier)
            if frontier.wire.sequence < 0 or (cursor is not None and not frontier.contains(cursor)):
                raise TranscriptChanged()
            traversal = LaterTranscript() if after is not None else EarlierTranscript()
            initial = cursor or frontier
            with reader.projection(source, page.routes) as project:
                consumed, records = traversal.read_records(
                    reader, project, source, initial, frontier,
                    max_messages=max_messages, max_bytes=max_bytes,
                )
            draft = reader.current_draft(page.root, thread.incarnation)
            start, end = traversal.bounds(initial, consumed)
            has_older = reader.has_older(start.offset) or start.wire.sequence > 0
            requested_earlier = traversal.request_earlier(reader, start.offset)
        events = traversal.finish(records)
        if requested_earlier and not events:
            raise CoordinationReadUnavailable("Waiting for the bridge's earlier transcript page")
        if page.inherited and not after and (cursor is None or cursor == frontier):
            events = (*events, *self._fork_start_events(thread))
        return (
            TranscriptPage(
                events, start, end,
                has_older,
                end != frontier,
                identity,
            ),
            draft,
        )

    def transcript_checkpoint(self, name: str) -> TranscriptCursor:
        registry = self.registry.snapshot()
        thread = registry.require(name)
        with thread.runtime.history_source(self.root, thread.incarnation).reading() as source:
            offset = source.cut().offset
        frontier = AssignedTranscriptSource.for_thread(self.root, thread, self.bus.log).frontier
        return thread.runtime.transcript_cursor(frontier, offset, source=thread.runtime)

    def record_turn_publication(
        self, *, lease: TurnLeaseFence, checkpoint: TranscriptCursor,
        routing: TurnRouting, published: tuple[MessageReference, ...],
    ) -> None:
        """Publish original native/wire relations before this exact turn retires."""
        self.routes.record_turn_publication(
            self.registry, self.bus.log, lease, checkpoint, routing, published
        )

