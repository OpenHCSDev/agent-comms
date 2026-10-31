"""Bounded transcript history and routing store ownership."""

from __future__ import annotations

import logging
from agent_comms.notification_assignment import NotificationSelection
from abc import abstractmethod
from collections.abc import Awaitable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Callable, ClassVar, NoReturn

from .agent_backend import RuntimeLocation
from .bus import Bus
from .bus_publication import Delivered
from .coordination_database import CoordinationStore
from .coordination_errors import CoordinationReadUnavailable, StaleRevision, TranscriptChanged
from .nominal.declared_family import DeclaredFamily
from .errors import RelationViolationError
from .external_transcript import ExternalTranscript
from .nominal.field_codec import ContentExclude, FieldCodec, WireName, projected
from .historical_views import HistorySource, HistorySourceKey
from .message_bus import MessageBus
from .message_reference import MessageReference
from .messaging import Messaging
from .native_runtime_input import PublishedReplyRevision
from .native_transcript import NativeTranscript, RecordsCut, SessionCut
from .native_output import NativeRecordLocation
from .notification_assignment import NotificationAssignment
from agent_comms.message_notification import NotificationWindow
from .notification_evidence import OwnerOutcomes
from .message_notification import MessageNotification
from .thread_presentation import ThreadPresentation
from .private_path import FileObservation, FileRevision
from .private_registry_guard import RegistryPrivacy
from .recorded_bus import RecordedBusIdentity
from .registration import Registration
from .registry_store import RegistryStore
from .routing import TurnRouting
from .sent_message import AssignedSourceIdentity
from .threads import Thread, ThreadProvenance
from .transcript_events import ForkOriginTranscript, ForkTaskTranscript, TranscriptEvent
from .transcript_routes import TranscriptRoutes
from .transcript_sources import (
    AssignedSourceCursor,
    AssignedTranscriptSource,
    EarlierTranscript,
    LaterTranscript,
    LiveAssignedTranscriptSource,
    NoWireFrontier,
    TranscriptDraft,
    TranscriptSource,
    TranscriptTraversal,
    WireFrontier,
)
from .turn_digests import InputAnswerRead, ThreadTurns, TurnBound, TurnDigestRead
from .turn_lease import TurnLeaseFence

if TYPE_CHECKING:
    from sqlite3 import Connection

    from .coordination_database import InvalidStore, MissingStore
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .thread_identity import ThreadIncarnation
    from .thread_text import ThreadParent, ThreadText
    from .view_unread import TranscriptReadState

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
    def mark_read(self, reads: TranscriptReadState, viewer: str, runtime: RuntimeLocation) -> None: ...

    def contains(self, other: TranscriptCursor) -> bool:
        return other.within(self)

    def same_position(self, other: TranscriptCursor) -> bool:
        return self == other

    def accepts_source(self, other: TranscriptCursor) -> bool:
        return self.session_file == other.session_file

    def within(self, other: TranscriptCursor) -> bool:
        return (self.accepts_source(other) and self.offset <= other.offset
                and self.wire.within(other.wire))

    def earlier_page(self) -> PagePosition:
        """The page that ends where this read position starts."""
        return EarlierThan(self)

    def later_page(self) -> PagePosition:
        """The page that starts where this read position ends."""
        return LaterThan(self)

    def page_limit(self) -> PageLimit:
        """A painted read position pins the page to the source it was read from."""
        return Through(self)

    def matches_runtime(self, runtime: RuntimeLocation) -> bool:
        return self.session_file == runtime.read_key

    def require_readable(
        self, runtime: RuntimeLocation, reader: TranscriptSource, size: int,
    ) -> None:
        if not self.matches_runtime(runtime) or not reader.valid_offset(self.offset, size):
            raise TranscriptChanged()

    def requires_commit(self) -> bool:
        """A changed transcript at a read position needs no commit before it is read again."""
        return False

    def painted_read[Result](self, acknowledge: Callable[[TranscriptCursor], Result],
                             nothing: Result) -> Result:
        """Choose painted acknowledgement work before any reader or write lock is opened."""
        return acknowledge(self)

    async def read_page(self, loader: Callable[..., Awaitable[TranscriptPage]],
                        position: PagePosition) -> TranscriptPage:
        return await loader(position=position, limit=self.page_limit())

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
class UnreadTranscriptCursor(TranscriptCursor):
    """No source has been read; this is not a source position or a read mark."""

    def contains(self, other: TranscriptCursor) -> bool:
        return False

    def within(self, other: TranscriptCursor) -> bool:
        return False

    def same_position(self, other: TranscriptCursor) -> bool:
        return False

    def accepts_source(self, other: TranscriptCursor) -> bool:
        return True

    def earlier_page(self) -> PagePosition:
        """Nothing read bounds no page: the latest one."""
        return LatestPage()

    def later_page(self) -> PagePosition:
        return LatestPage()

    def page_limit(self) -> PageLimit:
        """Nothing read pins no source window; the page's own frontier applies."""
        return AtFrontier()

    def requires_commit(self) -> bool:
        """A replaced source with nothing read must be committed before it is read again."""
        return True

    def painted_read[Result](self, acknowledge: Callable[[TranscriptCursor], Result],
                             nothing: Result) -> Result:
        return nothing

    def at_offset(self, offset: int) -> TranscriptCursor:
        return self

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return self

    def mark_read(self, reads: TranscriptReadState, viewer: str, runtime: RuntimeLocation) -> None:
        """Nothing has been drawn, so there is nothing to acknowledge."""


@dataclass(frozen=True, slots=True)
class NoNativeTranscriptCursor(TranscriptCursor):
    """A thread owns no native read mark, even while showing an inherited source."""

    source: RuntimeLocation = field(kw_only=True)
    offset: int = 0
    wire: WireFrontier = field(default_factory=NoWireFrontier)

    @property
    def session_file(self) -> str | None:
        return self.source.read_key

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return replace(self, wire=frontier)

    def mark_read(self, reads: TranscriptReadState, viewer: str, runtime: RuntimeLocation) -> None:
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

    def mark_read(self, reads: TranscriptReadState, viewer: str, runtime: RuntimeLocation) -> None:
        if self.session_file != runtime.read_key:
            raise TranscriptChanged()
        for key in runtime.read_keys():
            reads.mark_read(viewer, key, self.offset, NativeTranscript(Path(key)).cut())


@dataclass(frozen=True)
class NativeCommittedPrefix:
    """Original committed native bytes, without unrelated snapshot revisions."""

    original: NativeRecordLocation
    records_cut: SessionCut
    source: AssignedSourceIdentity

    @property
    def page_bound(self) -> NativeTranscriptCursor:
        return NativeTranscriptCursor(self.original.session_file, self.original.session_entry_end)

    def contains_native_prefix(self, current: TranscriptReadIdentity) -> bool:
        current_source = AssignedSourceIdentity(current.root, current.thread.incarnation)
        return (
            (self.source, self.original.session_file) == (current_source, current.runtime.read_key)
            and self.page_bound.within(current.page_bound)
            and self.records_cut.holds(current.records_cut, self.original.session_entry_end)
        )


@dataclass(frozen=True, slots=True)
class WireTranscriptCursor(NativeTranscriptCursor):
    wire: AssignedSourceCursor = field()


@dataclass(frozen=True)
class PagePosition(DeclaredFamily, affix=""):
    """Where a bounded transcript page starts: the latest page, or one side of a read cursor."""

    def require_readable(
        self, runtime: RuntimeLocation, reader: TranscriptSource, size: int,
    ) -> None:
        """The latest page has no cursor to validate."""

    def within(self, frontier: TranscriptCursor) -> bool:
        return True

    def initial(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return frontier

    def traversal(self) -> TranscriptTraversal:
        return EarlierTranscript()

    def shows_fork_start(self, frontier: TranscriptCursor) -> bool:
        """An inherited thread's fork origin closes its oldest page; the latest page reaches it."""
        return True

    def later_only(self) -> PagePosition:
        """A re-read under a new limit keeps only a later-than cursor; any other page starts latest."""
        return LatestPage()


@dataclass(frozen=True, slots=True)
class LatestPage(PagePosition):
    """The newest page up to the frontier."""


@dataclass(frozen=True, slots=True)
class CursorPage(PagePosition, declared_name=None):
    """A page beside a read cursor, which must belong to the page's source and fit its cut."""

    cursor: TranscriptCursor

    def require_readable(
        self, runtime: RuntimeLocation, reader: TranscriptSource, size: int,
    ) -> None:
        self.cursor.require_readable(runtime, reader, size)

    def within(self, frontier: TranscriptCursor) -> bool:
        return frontier.contains(self.cursor)

    def initial(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return self.cursor


@dataclass(frozen=True, slots=True)
class EarlierThan(CursorPage):
    def shows_fork_start(self, frontier: TranscriptCursor) -> bool:
        return self.cursor == frontier


@dataclass(frozen=True, slots=True)
class LaterThan(CursorPage):
    def traversal(self) -> TranscriptTraversal:
        return LaterTranscript()

    def shows_fork_start(self, frontier: TranscriptCursor) -> bool:
        return False

    def later_only(self) -> PagePosition:
        return self


@dataclass(frozen=True)
class PageLimit(DeclaredFamily, affix=""):
    """How far a page's source reaches: its live frontier, or a cursor already painted."""

    def bound(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return frontier

    def accepts_runtime(self, runtime: RuntimeLocation) -> bool:
        """An unpinned page uses the thread's first available transcript source."""
        return True


@dataclass(frozen=True, slots=True)
class AtFrontier(PageLimit):
    """The page reaches the source's current frontier."""


@dataclass(frozen=True, slots=True)
class Through(PageLimit):
    """The page reaches no further than a cursor already painted, in that cursor's source."""

    cursor: TranscriptCursor

    def bound(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return self.cursor

    def accepts_runtime(self, runtime: RuntimeLocation) -> bool:
        return self.cursor.matches_runtime(runtime)


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
class ResolvedPageSource:
    """The retained stores one thread's transcript page is read from."""

    thread: Thread
    runtime: Annotated[RuntimeLocation, WireName("session_file")]
    inherited: bool
    root: Path
    bus: Bus
    routes: TranscriptRoutes
    records: TranscriptSource
    identity: Callable[..., TranscriptReadIdentity]
    projection: TranscriptSource


class PageSource(DeclaredFamily, affix="PageSource"):
    """The chosen live or archived namespace owns its acquisition and witness member."""

    @abstractmethod
    def resolve(self, owner: Transcripts, name: str, limit: PageLimit) -> ResolvedPageSource: ...


@dataclass(frozen=True)
class LivePageSource(PageSource):
    def resolve(self, owner: Transcripts, name: str, limit: PageLimit) -> ResolvedPageSource:
        registry = owner.registry.snapshot()
        thread, runtime, inherited = owner._thread_transcript_source(name, limit, registry=registry)
        records = runtime.history_source(owner.root, thread.incarnation)
        return ResolvedPageSource(
            thread, runtime, inherited, owner.root, owner.bus.bus, owner.routes, records,
            LiveTranscriptReadIdentity,
            LiveAssignedTranscriptSource.for_thread(owner.root, thread, owner.bus.bus))


@dataclass(frozen=True)
class RecordedPageSource(PageSource):
    source: HistorySourceKey

    def resolve(self, owner: Transcripts, name: str, limit: PageLimit) -> ResolvedPageSource:
        source = HistorySource.require(owner.bus.history.sources(), self.source)
        thread = source.provenance.require(name)
        runtime = thread.runtime
        if not limit.accepts_runtime(runtime):
            raise TranscriptChanged()
        root = Path(source.root)
        bus = Bus(Bus.path_in(root))
        records = runtime.history_source(root, thread.incarnation)
        return ResolvedPageSource(
            thread, runtime, False, root, bus, TranscriptRoutes(root), records,
            partial(RecordedTranscriptReadIdentity, source=source),
            AssignedTranscriptSource.for_recorded(root, thread, bus, records))


@dataclass(frozen=True)
class TranscriptReadIdentity(DeclaredFamily, affix="TranscriptReadIdentity"):
    """All canonical inputs to a bounded native page, including annotations."""

    root: str
    requested_name: str
    thread: Annotated[ThreadProvenance, ContentExclude()]
    runtime: RuntimeLocation
    records_cut: Annotated[RecordsCut, ContentExclude()]
    route_revision: FileObservation
    bus_revision: Annotated[int, ContentExclude()]
    bus_identity: RecordedBusIdentity
    coordination_revision: Annotated[FileObservation, ContentExclude()]
    reply_revision: PublishedReplyRevision
    read_revision: Annotated[FileObservation, ContentExclude()]
    wire_frontier: WireFrontier
    position: PagePosition
    limit: PageLimit

    def __post_init__(self) -> None:
        # A decoded witness reads its transcript under the root that published its thread.
        bound = self.thread.session_file.bound_to(self.transcript_root)
        if bound is not self.thread.session_file:
            object.__setattr__(self, "thread", replace(self.thread, session_file=bound))

    @property
    def transcript_root(self) -> Path:
        return Path(self.root)

    @projected(view=ContentExclude, name="thread")
    def content_thread(self) -> ThreadIncarnation:
        return self.thread.incarnation

    @property
    def page_source(self) -> PageSource:
        return LivePageSource()

    @abstractmethod
    def namespace(self, owner: Transcripts) -> RegistryProvenance:
        """Reacquire this exact source without substituting a live namespace."""

    def publish_turn(self, publish: Callable[[str, Thread], None]) -> None:
        """Recorded evidence cannot settle a turn or permit new input."""

    def recapture(self, owner: Transcripts) -> TranscriptReadIdentity:
        return owner.recapture_page_identity(self)

    def require_binding(self, owner: Transcripts, name: str) -> None:
        registry = self.namespace(owner)
        requested = registry.threads.get(registry.canonical_name(name))
        if requested is None:
            raise StaleRevision("Transcript request has no current source")
        if self.thread.incarnation.resolved(registry) != requested.incarnation:
            raise StaleRevision("Transcript witness belongs to another original thread")

    @projected(view=ContentExclude, name="bus")
    def content_bus(self) -> RecordedBusIdentity:
        # Appends beyond this page's assigned messages do not change its content.
        # Replacing the bus file does.
        return self.bus_identity

    @property
    def page_bound(self) -> TranscriptCursor:
        """The last native byte and assigned message included in this page."""
        return self.limit.bound(self.thread.runtime.transcript_cursor(
            self.wire_frontier, self.records_cut.offset, source=self.runtime))

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
            FieldCodec.project(self, ContentExclude) == FieldCodec.project(other, ContentExclude)
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
    thread: Annotated[Thread, ContentExclude()]

    @projected(view=ContentExclude, name="thread")
    def content_thread(self) -> tuple[ThreadIncarnation, ThreadParent, ThreadText]:
        return self.thread.incarnation, self.thread.parent, self.thread.task

    def namespace(self, owner: Transcripts) -> RegistryProvenance:
        return owner.registry.snapshot()

    def publish_turn(self, publish: Callable[[str, Thread], None]) -> None:
        publish(self.root, self.thread)


@dataclass(frozen=True, slots=True)
class RecordedTranscriptReadIdentity(TranscriptReadIdentity):
    source: Annotated[HistorySource, ContentExclude()] = field(kw_only=True)

    @property
    def transcript_root(self) -> Path:
        return Path(self.source.original_root)

    @property
    def page_source(self) -> PageSource:
        return RecordedPageSource(self.source.key)

    @projected(view=ContentExclude, name="source")
    def content_source(self) -> tuple[str, str, RecordedBusIdentity, RecordedBusIdentity, int, FileObservation]:
        return (self.source.key, self.source.original_root, self.source.bus_identity,
                self.source.snapshot_bus_identity, self.source.snapshot_bus_revision,
                self.source.snapshot_registry_revision)

    def namespace(self, owner: Transcripts) -> RegistryProvenance:
        current = HistorySource.require(owner.bus.history.sources(), self.source.key)
        if current != self.source:
            raise StaleRevision("Transcript witness belongs to another recorded source")
        return current.provenance

    def recapture(self, owner: Transcripts) -> TranscriptReadIdentity:
        self.namespace(owner)
        return TranscriptReadIdentity.recapture(self, owner)


@dataclass(frozen=True, slots=True)
class TranscriptRead:
    """Deferred canonical read retaining its resolved source and captured identity."""

    owner: Transcripts
    identity: TranscriptReadIdentity
    source: ResolvedPageSource
    # Required viewer reads stay pending in the read lifecycle. Their SQL
    # attempt must not consume that wait by blocking a presentation worker.
    source_wait: ClassVar[float] = 0

    @classmethod
    @contextmanager
    def publication_source(cls, root: Path) -> Iterator[Connection | None]:
        privacy = RegistryStore.at(root).privacy()
        store = CoordinationStore.observed(CoordinationStore.path_in(root))
        with store.observing(cls, privacy) as db:
            yield db

    @classmethod
    def capture(cls, db: Connection, privacy: RegistryPrivacy) -> Connection | None:
        return privacy.publication_connection(db)

    @classmethod
    def missing_store(cls, store: MissingStore, privacy: RegistryPrivacy) -> None:
        return None

    @classmethod
    def invalid_store(cls, store: InvalidStore, privacy: RegistryPrivacy) -> NoReturn:
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
            position=identity.position,
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
    sources: tuple[Delivered, ...]
    sent_sources: tuple[Delivered, ...]

    def presentation(self, ordinary: ThreadPresentation) -> AcquiredThreadPresentation:
        """Project incoming and sent outcomes from this one acquired source window."""
        transcripts = self.read.owner
        sources = tuple({source.message.reference: source
                         for source in (*self.sources, *self.sent_sources)}.values())
        viewer = OwnerOutcomes.of(self.read.identity.thread.incarnation)
        notification_read = NotificationAssignment.read(transcripts.root, NotificationSelection.of(viewer, sources))
        projected = MessageNotification.delivery_window(
            transcripts.root, transcripts.registry, notification_read)
        incoming = tuple(
            replace(notification, message=source.envelope)
            for source in self.sources
            for notification in projected.outcomes[source.message.seq, source.message.message_id]
        )
        return AcquiredThreadPresentation(
            **{declared.name: getattr(ordinary, declared.name)
               for declared in fields(ThreadPresentation)},
            notifications=incoming,
            read_identity=self.read.identity,
            notification_window=projected,
            incoming_references=tuple(source.message.reference for source in self.sources),
            sent_references=tuple(source.message.reference for source in self.sent_sources))



@dataclass(frozen=True, slots=True)
class NotifiedThreadPresentation(ThreadPresentation):
    """A presentation holding the notifications and read identity of its acquired page."""

    notifications: tuple[MessageNotification, ...] = field(default=())
    read_identity: TranscriptReadIdentity | None = field(default=None)

    def same_observation(self, other: ThreadPresentation) -> bool:
        """Equal view and turn evidence, not merely equal global store revisions.

        Keep the exact registered thread and binding used for turn settlement.
        The transcript owner decides whether its page changed; unrelated bus,
        read-mark and coordinator writes do not create another page publication.
        """
        if type(other) is not type(self) or replace(self, read_identity=other.read_identity) != other:
            return False
        original, current = self.read_identity, other.read_identity
        if original is None or current is None:
            return original is current
        return original.thread == current.thread and original.same_snapshot(current)


@dataclass(frozen=True, slots=True)
class AcquiredThreadPresentation(NotifiedThreadPresentation):
    """A presentation together with the notification evidence it was acquired from.

    The evidence stays with the reader that acquired it, for its change checks and sent
    summaries; a viewer receives only the published NotifiedThreadPresentation."""

    incoming_references: tuple[MessageReference, ...] = field(kw_only=True)
    sent_references: tuple[MessageReference, ...] = field(kw_only=True)
    notification_window: NotificationWindow = field(kw_only=True)

    def published(self) -> ThreadPresentation:
        return NotifiedThreadPresentation(**{declared.name: getattr(self, declared.name)
                                             for declared in fields(NotifiedThreadPresentation)})

    def sent_notifications(
        self,
    ) -> tuple[tuple[MessageReference, tuple[MessageNotification, ...]], ...]:
        """Each recent sent source with its outcomes from this presentation's captured window."""
        return tuple(
            (reference,
             self.notification_window.outcomes.get((reference.seq, reference.message_id), ()))
            for reference in self.sent_references
        )

    def collect_revision_input(self, name: str,
                               readable: dict[str, AcquiredThreadPresentation]) -> None:
        readable[name] = self

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError(
            "AcquiredThreadPresentation keeps private delivery evidence with its reader; "
            "send its published() ThreadPresentation")


class Transcripts:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, messaging: Messaging):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.messaging = messaging
        self.routes = TranscriptRoutes(root)
        self.page_reads = 0

    def answered_inputs(
        self, thread: Thread, references: tuple[MessageReference, ...], privacy: RegistryPrivacy,
    ) -> frozenset[MessageReference]:
        """The same source family owns bounded answer evidence for native and external turns."""
        return InputAnswerRead(self.root, thread, references, privacy).read()

    def bind_page_read(
        self,
        name: str,
        identity: TranscriptReadIdentity,
        *,
        position: PagePosition = LatestPage(),
        limit: PageLimit = AtFrontier(),
    ) -> TranscriptRead:
        """Bind the original published witness to this actual source request."""
        if identity.root != str(self.root):
            raise StaleRevision("Transcript witness belongs to another root")
        if (identity.position, identity.limit) != (position, limit):
            raise StaleRevision("Transcript witness belongs to another page window")
        identity.require_binding(self, name)
        source = identity.page_source.resolve(self, name, Through(identity.page_bound))
        return TranscriptRead(self, identity, source)

    def capture_native_output(
        self, name: str, location: NativeRecordLocation,
    ) -> NativeCommittedPrefix:
        page = LivePageSource().resolve(self, name, AtFrontier())
        cut = page.records.committed_output_cut(location)
        original = NativeRecordLocation(**{
            item.name: getattr(location, item.name)
            for item in fields(NativeRecordLocation) if item.init
        })
        return NativeCommittedPrefix(
            original, cut, AssignedSourceIdentity(str(page.root), page.thread.incarnation),
        )

    def capture_page_read(
        self,
        name: str,
        *,
        position: PagePosition = LatestPage(),
        limit: PageLimit = AtFrontier(),
        page_source: PageSource = LivePageSource(),
    ) -> TranscriptRead:
        page = page_source.resolve(self, name, limit)
        frontier = page.projection.frontier
        identity = self._capture_page_identity(
            page, name, frontier, position=position, limit=limit,
        )
        return TranscriptRead(self, identity, page)

    def capture_page_window(
        self,
        name: str,
        *,
        source_limit: int = 1,
        sent_source_limit: int = 0,
        position: PagePosition = LatestPage(),
        limit: PageLimit = AtFrontier(),
        page_source: PageSource = LivePageSource(),
    ) -> TranscriptSourceWindow:
        """Expose the source owner's complete identity without parsing its page.

        Native bytes alone are insufficient: input display/routing and older
        bus evidences contribute to the same projection. Registry-owned thread
        metadata also carries inherited fork text and source selection.
        """
        if not 1 <= source_limit <= NotificationSelection.window_limit:
            raise ValueError("Transcript source capture requires a bounded window")
        if not 0 <= sent_source_limit <= NotificationSelection.window_limit:
            raise ValueError("Sent source capture requires a bounded window")
        page = page_source.resolve(self, name, limit)
        captured = page.projection.window(limit=source_limit, sent_limit=sent_source_limit)
        identity = self._capture_page_identity(
            page, name, captured.cursor, position=position, limit=limit,
        )
        return TranscriptSourceWindow(
            TranscriptRead(self, identity, page), captured.sources, captured.sent_sources
        )

    def recapture_page_identity(
        self, identity: TranscriptReadIdentity,
    ) -> TranscriptReadIdentity:
        """Recapture identity facts without reconstructing presentation source windows."""
        page = identity.page_source.resolve(self, identity.requested_name, identity.limit)
        frontier = page.projection.frontier
        return self._capture_page_identity(
            page,
            identity.requested_name,
            frontier,
            position=identity.position,
            limit=identity.limit,
        )

    def _capture_page_identity(
        self,
        page: ResolvedPageSource,
        name: str,
        wire_frontier: WireFrontier,
        *,
        position: PagePosition,
        limit: PageLimit,
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
            page.bus.revision(),
            RecordedBusIdentity.capture(page.bus.path),
            FileRevision.capture(CoordinationStore.source_paths(root)[0]),
            page.projection.reply_revision(records),
            FileRevision.capture(root / self.bus.reads.path.name),
            wire_frontier,
            position,
            limit,
        )

    def turns(self, name: str, bound: TurnBound) -> ThreadTurns:
        return TurnDigestRead(self.root, self.registry.snapshot().require(name), bound).read()

    def external(self, name: str) -> ExternalTranscript:
        """Where a CLI bridge publishes the items of its sessionless bus thread."""
        page = LivePageSource().resolve(self, name, AtFrontier())
        return page.runtime.external_records(page.root, page.thread.incarnation)

    def _thread_transcript_source(
        self, name: str, limit: PageLimit, *, registry: RegistrySnapshot
    ) -> tuple[Thread, RuntimeLocation, bool]:
        """Resolve a temporary inherited view without changing runtime ownership."""
        thread = registry.require(name)
        for location, inherited in thread.runtime.history_locations(thread, registry, set()):
            if limit.accepts_runtime(location):
                return thread, location, inherited
        raise TranscriptChanged()

    @staticmethod
    def _fork_start_events(thread: Thread) -> tuple[TranscriptEvent, ...]:
        return tuple(
            event for parent in thread.parent.names() for event in (
                ForkOriginTranscript(f"Forked from @{parent}. This thread started with:",
                                     timestamp=thread.incarnation.wire_birth),
                *(ForkTaskTranscript(task, timestamp=thread.incarnation.wire_birth)
                  for task in thread.task.texts()),
            )
        )

    def _thread_transcript_page(
        self,
        page: ResolvedPageSource,
        *,
        identity: TranscriptReadIdentity,
        position: PagePosition,
        max_messages: int,
        max_bytes: int,
        through: TranscriptCursor,
    ) -> tuple[TranscriptPage, TranscriptDraft]:
        """Read page and draft while their record owner retains one source lifecycle; the
        page reaches through its witness's painted bound."""
        self.page_reads += 1
        thread, runtime = page.thread, page.runtime
        source = page.projection
        with page.records.reading() as reader:
            through.require_readable(runtime, reader, reader.cut().offset)
            size = through.offset
            position.require_readable(runtime, reader, size)
            frontier = through
            source.require_cursor(frontier)
            if frontier.wire.sequence < 0 or not position.within(frontier):
                raise TranscriptChanged()
            traversal = position.traversal()
            initial = position.initial(frontier)
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
        if page.inherited and position.shows_fork_start(frontier):
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
        frontier = AssignedTranscriptSource.for_thread(self.root, thread, self.bus.bus).frontier
        return thread.runtime.transcript_cursor(frontier, offset, source=thread.runtime)

    def record_turn_publication(
        self, *, lease: TurnLeaseFence, checkpoint: TranscriptCursor,
        routing: TurnRouting, published: tuple[MessageReference, ...],
    ) -> None:
        """Publish original native/wire relations before this exact turn retires."""
        self.routes.record_turn_publication(
            self.registry, self.bus.bus, lease, checkpoint, routing, published
        )
