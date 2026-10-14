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
from .thread_provenance import ThreadProvenance
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
from .transcript_events import NoticeTranscript, TranscriptEvent, UserTranscript
from .transcript_routes import TranscriptRoutes, TranscriptRouteRevision
from .transcript_receipts import AssignedSourceCursor, AssignedSourceIdentity
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

    def metadata(self) -> dict[str, object]:
        return {
            field.name: FieldCodec.encode(getattr(self, field.name))
            for field in fields(self)
            if field.name != "events"
        }


@dataclass(frozen=True, slots=True)
class PageSource:
    """The stores one thread's transcript page is read from."""

    thread: Thread
    session_file: str
    inherited: bool
    root: Path
    log: WireLog
    routes: TranscriptRoutes
    identity: Callable[..., TranscriptReadIdentity]

    @property
    def records(self) -> NativeTranscript | ExternalTranscript:
        """A native session's records, or the items its CLI publishes when it has none."""
        if self.session_file:
            return NativeTranscript(Path(self.session_file))
        return ExternalTranscript(self.root, self.thread.incarnation)


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
    receipt_frontier: AssignedSourceCursor
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

    def recapture(self, owner) -> TranscriptRead:
        return owner.capture_page_read(
            self.requested_name, before=self.before, after=self.after, through=self.through,
            historical_source=self.historical_source,
        )

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
        return PageSource(thread, session_file, inherited,
                          owner.root, owner.bus.log, owner.routes, cls)

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

        source = HistorySource.require(owner.bus.history, historical_source)
        thread = source.provenance.require(name)
        session_file = thread.session_file or ""
        if source_file is not None and source_file != session_file:
            raise TranscriptChanged()
        root = Path(source.root)
        return PageSource(thread, session_file, False, root,
                          WireLog(root / "bus.jsonl"), TranscriptRoutes(root),
                          partial(cls, source=source))

    def namespace(self, owner):
        current = HistorySource.require(owner.bus.history, self.source.key)
        if current != self.source:
            raise StaleRevision("Transcript witness belongs to another recorded source")
        return current.provenance

    def recapture(self, owner) -> TranscriptRead:
        self.namespace(owner)
        return TranscriptReadIdentity.recapture(self, owner)


@dataclass(frozen=True, slots=True)
class TranscriptRead:
    """Deferred canonical read, fenced to the identity captured by its owner."""

    owner: Transcripts
    identity: TranscriptReadIdentity
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
        with CoordinationStore.observing(database, lock_timeout=cls.source_wait) as db:
            assert_native_runtime_schema(db)
            _assert_response_schema(db)
            yield db

    def current(self) -> bool:
        return self.identity == self.current_identity()

    def content_current(self) -> bool:
        return self.identity.same_content(self.current_identity())

    def current_identity(self) -> TranscriptReadIdentity:
        return self.identity.recapture(self.owner).identity

    def read(self) -> TranscriptPage:
        if not self.content_current():
            raise StaleRevision("Transcript read inputs changed before preparation")
        identity = self.identity
        page = self.owner.thread_transcript_page(
            identity.requested_name,
            before=identity.before,
            after=identity.after,
            through=identity.page_bound,
            historical_source=identity.historical_source,
        )
        if not self.content_current():
            raise StaleRevision("Transcript read inputs changed during preparation")
        return page


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
        return TranscriptRead(self, identity)

    def capture_page_read(
        self,
        name: str,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        through: TranscriptCursor | None = None,
        historical_source: str | None = None,
    ) -> TranscriptRead:
        return self.capture_page_window(
            name, before=before, after=after, through=through, historical_source=historical_source,
        ).read

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
        from .transcript_receipts import AssignedTranscriptSource

        root, thread, records = page.root, page.thread, page.records
        source = AssignedTranscriptSource.for_thread(root, thread, page.log)
        captured = source.window(limit=source_limit, sent_limit=sent_source_limit)
        read = TranscriptRead(
            self,
            page.identity(
                str(self.root),
                name,
                thread,
                page.session_file,
                records.cut(),
                page.routes.revision(),
                FileRevision.capture(page.log.path),
                FileRevision.capture(root / "coordination.sqlite3"),
                FileRevision.capture(root / "coordination.sqlite3-wal"),
                records.reply_revision(root, thread),
                FileRevision.capture(root / self.bus.reads.path.name),
                captured.cursor,
                before,
                after,
                through,
            ),
        )
        return TranscriptSourceWindow(read, captured.sources, captured.sent_sources)

    def external(self, name: str) -> ExternalTranscript:
        """Where a CLI bridge publishes the items of its sessionless bus thread."""
        page = self._page_source(name, None, None)
        if page.session_file:
            raise ValueError(f"@{name} reads a native session; a CLI cannot publish its transcript")
        return page.records

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
        self.page_reads += 1
        if before is not None and after is not None:
            raise ValueError("Choose one transcript paging direction.")
        if max_messages <= 0 or max_bytes <= 0:
            raise ValueError("Transcript page budgets must be positive.")
        # A mounted inherited window remains pinned to its ancestor and byte
        # boundary when the child persists its own session. New unpinned reads
        # select the child's file; existing scroll cursors keep working.
        page = self._page_source(name, through, historical_source)
        thread, session_file, reader = page.thread, page.session_file, page.records
        cursor = before or after
        if cursor and (cursor.session_file != session_file or cursor.offset < 0):
            raise TranscriptChanged()
        size = reader.cut().offset
        if cursor and cursor.offset > size:
            raise TranscriptChanged()
        if through is not None:
            if through.session_file != session_file or not 0 <= through.offset <= size:
                raise TranscriptChanged()
            size = through.offset
            if cursor and cursor.offset > size:
                raise ValueError("Cursor is outside the transcript window.")
        from .transcript_receipts import (
            AssignedTranscriptSource,
            EarlierTranscript,
            LaterTranscript,
        )

        receipts = AssignedTranscriptSource.for_thread(page.root, thread, page.log)
        frontier = through or TranscriptCursor(session_file, size, receipts.frontier)
        frontier.require_source(AssignedSourceIdentity(str(page.root), thread.incarnation))
        if frontier.wire_seq < 0 or (cursor is not None and not frontier.contains(cursor)):
            raise ValueError("Cursor is outside the combined transcript source")
        traversal = LaterTranscript() if after is not None else EarlierTranscript()
        initial = cursor or frontier
        with reader.projection(receipts, page.routes) as project:
            consumed, records = traversal.read_records(
                reader, project, receipts, initial, frontier,
                max_messages=max_messages, max_bytes=max_bytes,
            )
        start, end = traversal.bounds(initial, consumed)
        events = traversal.finish(records)
        if page.inherited and not after and (cursor is None or cursor == frontier):
            events = (*events, *self._fork_start_events(thread))
        return TranscriptPage(
            events, start, end,
            start.offset > 0 or start.wire_seq > 0,
            end != frontier,
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

