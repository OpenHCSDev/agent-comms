"""Bounded transcript history and routing store ownership."""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, ClassVar, NoReturn

from agent_comms.agent_backend import RuntimeLocation
from agent_comms.coordination.database import (
    CoordinationStore,
    InvalidStore,
    MissingStore,
)
from agent_comms.coordination.errors import (
    CoordinationReadUnavailable,
    ReloadRequired,
    StaleRevision,
    TranscriptChanged,
)
from agent_comms.errors import RelationViolationError
from agent_comms.execution.history_view import HistoryView, OwnHistory
from agent_comms.execution.native_entries import NativeEvidenceScope
from agent_comms.execution.native_input_owner import RegistryOwner
from agent_comms.execution.native_output import NativeRecordLocation
from agent_comms.execution.native_record_file import RecordsCut, SessionCut
from agent_comms.execution.native_runtime_input import PublishedReplyRevision
from agent_comms.execution.native_session_files import NativeSessionFiles
from agent_comms.execution.transcript_cursor_reads import (
    MarksSourceRead,
    TranscriptRuntime,
)
from agent_comms.execution.transcript_routes import TranscriptRoutes
from agent_comms.execution.transcript_sources import (
    AssignedTranscriptSource,
    AtFrontier,
    ExternalTranscript,
    FileTranscriptCursor,
    LatestPage,
    LiveAssignedTranscriptSource,
    NativeTranscript,
    NativeTranscriptCursor,
    NoWireFrontier,
    PageLimit,
    PagePosition,
    Through,
    TranscriptCursor,
    TranscriptSource,
    WireFrontier,
)
from agent_comms.execution.turn_bound import TurnBound
from agent_comms.execution.turn_digests import (
    InputAnswerRead,
    ThreadTurns,
    TurnDigestRead,
)
from agent_comms.execution.turn_lease import TurnLeaseFence
from agent_comms.input_disposition import InputDispositions
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.historical_views import HistorySource
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.message_notification import (
    MessageNotification,
    NotificationWindow,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_evidence import OwnerOutcomes
from agent_comms.messaging.page_traversal import HistorySourceKey
from agent_comms.messaging.recorded_bus import RecordedBusIdentity
from agent_comms.messaging.routing import TurnRouting
from agent_comms.messaging.sent_message import AssignedSourceIdentity
from agent_comms.messaging.sent_reports import CapturedSentReports
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    ContentExclude,
    FieldCodec,
    MalformedRecord,
    WireName,
    projected,
)
from agent_comms.notification_assignment import (
    AssignmentSource,
    NotificationSelection,
)
from agent_comms.registry.private_registry_guard import RegistryPrivacy
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_provenance import RegistryProvenance
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.registry_tables import RegistryRowRevision
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_presentation import ThreadPresentation
from agent_comms.registry.thread_text import ThreadParent, ThreadText
from agent_comms.registry.threads import Thread, ThreadProvenance
from agent_comms.registry.view_unread import TranscriptReadState
from agent_comms.storage.private_path import (
    FileIdentity,
    FileObservation,
    FileRevision,
    MissingFile,
)
from agent_comms.transcript.transcript_drafts import TranscriptDraft
from agent_comms.transcript.transcript_events import (
    ForkOriginTranscript,
    ForkTaskTranscript,
    TranscriptEvent,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NoNativeTranscriptCursor(FileTranscriptCursor):
    """A thread owns no native read mark, even while showing an inherited source."""

    source: RuntimeLocation = field(kw_only=True)
    offset: int = 0
    wire: WireFrontier = field(default_factory=NoWireFrontier)
    file: FileIdentity | MissingFile = field(kw_only=True)

    @property
    def session_file(self) -> str | None:
        return self.source.read_key

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return replace(self, wire=frontier)

    def mark_read(self, reads: MarksSourceRead[SessionCut], viewer: str, runtime: TranscriptRuntime) -> None:
        """There is no painted native transcript to acknowledge."""


@dataclass(frozen=True)
class NativeCommittedPrefix:
    """Original committed native bytes, without unrelated snapshot revisions."""

    original: NativeRecordLocation
    records_cut: SessionCut
    source: AssignedSourceIdentity

    @property
    def page_bound(self) -> NativeTranscriptCursor:
        return NativeTranscriptCursor(self.original.session_file, self.original.session_entry_end,
                                      file=self.records_cut.file_identity())

    def contains_native_prefix(self, current: TranscriptReadIdentity) -> bool:
        current_source = AssignedSourceIdentity(current.root, current.thread.incarnation)
        return (
            (self.source, self.original.session_file) == (current_source, current.runtime.read_key)
            and self.page_bound.within(current.page_bound)
            and self.records_cut.holds(current.records_cut, self.original.session_entry_end)
        )


@dataclass(frozen=True, slots=True)
class TranscriptPage(CapturedSentReports):
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
    view: HistoryView
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
        thread, runtime, view = owner._thread_transcript_source(name, limit, registry=registry)
        records = runtime.history_source(owner.root, thread.incarnation)
        return ResolvedPageSource(
            thread, runtime, view, owner.root, owner.bus.bus, owner.routes, records,
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
            thread, runtime, OwnHistory(), root, bus, TranscriptRoutes(root), records,
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
            raise ReloadRequired("Transcript request has no current source")
        if self.thread.incarnation.resolved(registry) != requested.incarnation:
            raise ReloadRequired("Transcript witness belongs to another original thread")

    @projected(view=ContentExclude, name="bus")
    def content_bus(self) -> RecordedBusIdentity:
        # Appends beyond this page's assigned messages do not change its content.
        # Replacing the bus file does.
        return self.bus_identity

    @property
    def page_bound(self) -> TranscriptCursor:
        """The last native byte and assigned message included in this page."""
        return self.limit.bound(self.thread.runtime.transcript_cursor(
            self.wire_frontier, self.records_cut, source=self.runtime))

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
    def content_source(self) -> tuple[str, str, RecordedBusIdentity, RecordedBusIdentity, int, RegistryRowRevision]:
        return (self.source.key, self.source.original_root, self.source.bus_identity,
                self.source.snapshot_bus_identity, self.source.snapshot_bus_revision,
                self.source.snapshot_registry_revision)

    def namespace(self, owner: Transcripts) -> RegistryProvenance:
        current = HistorySource.require(owner.bus.history.sources(), self.source.key)
        if current != self.source:
            raise ReloadRequired("Transcript witness belongs to another recorded source")
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

    def presentation(self, ordinary: ThreadPresentation, assignments: AssignmentSource) -> AcquiredThreadPresentation:
        """Project incoming and sent outcomes from this one acquired source window."""
        transcripts = self.read.owner
        sources = tuple({source.message.reference: source
                         for source in (*self.sources, *self.sent_sources)}.values())
        viewer = OwnerOutcomes.of(self.read.identity.thread.incarnation)
        notification_read = assignments.read_assignments(NotificationSelection.of(viewer, sources))
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

    def published(self) -> NotifiedThreadPresentation:
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
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
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
            raise ReloadRequired("Transcript witness belongs to another root")
        if (identity.position, identity.limit) != (position, limit):
            raise ReloadRequired("Transcript witness belongs to another page window")
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
    ) -> tuple[Thread, RuntimeLocation, HistoryView]:
        """Resolve a temporary inherited view without changing runtime ownership."""
        thread = registry.require(name)
        for location, view in thread.runtime.history_locations(thread, registry, set()):
            if limit.accepts_runtime(location):
                return thread, location, view
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
            current = reader.cut()
            through.require_readable(runtime, reader, current, current.offset)
            position.require_readable(runtime, reader, current, through.offset)
            frontier = through
            source.require_cursor(frontier)
            if frontier.wire.sequence < 0 or not position.within(frontier):
                raise TranscriptChanged()
            traversal = position.traversal()
            initial = position.initial(frontier)
            with reader.projection(source, page.routes) as project:
                consumed, records = traversal.read_records(
                    reader, project, source, initial, frontier, page.view,
                    max_messages=max_messages, max_bytes=max_bytes,
                )
            draft = reader.current_draft(page.root, thread.incarnation)
            start, end = traversal.bounds(initial, consumed)
            has_older = reader.has_older(start.offset) or start.wire.sequence > 0
            requested_earlier = traversal.request_earlier(reader, start.offset)
        events = traversal.finish(records)
        if requested_earlier and not events:
            raise CoordinationReadUnavailable("Waiting for the bridge's earlier transcript page")
        events = page.view.framed(
            events, partial(self._fork_start_events, thread),
            ends_at_fork=position.shows_fork_start(frontier), reaches_start=not has_older)
        return (
            TranscriptPage(
                events, start, end,
                has_older,
                end != frontier,
                identity,
                sent_resolutions=source.capture_sent_reports(
                    report for event in events for report in event.sent_reports()),
            ),
            draft,
        )

    def transcript_checkpoint(self, name: str) -> TranscriptCursor:
        registry = self.registry.snapshot()
        thread = registry.require(name)
        with thread.runtime.history_source(self.root, thread.incarnation).reading() as source:
            cut = source.cut()
        frontier = AssignedTranscriptSource.for_thread(self.root, thread, self.bus.bus).frontier
        return thread.runtime.transcript_cursor(frontier, cut, source=thread.runtime)

    def record_turn_publication(
        self, *, lease: TurnLeaseFence, checkpoint: TranscriptCursor,
        routing: TurnRouting, published: tuple[MessageReference, ...],
    ) -> None:
        """Join a completed native producer to its original committed wire rows.

        Finish reading native history before locking the sources. Existing wire,
        registry and input owners remain held until durable annotations commit.
        A partial successful send subset remains published if a later send fails.
        """
        if not routing.requires_annotation(published):
            return
        initial = self.registry.snapshot()
        incarnation = lease.identity.incarnation.resolved(initial)
        thread = RegistryOwner.capture_local(initial, incarnation.name).thread
        if thread.turn_lease != lease:
            raise RelationViolationError("Native publication has no current original turn")
        path = Path(thread.runtime.path)
        revision = FileRevision.capture(path).require(
            RelationViolationError("Native publication has no retained session"))
        reader = NativeTranscript(path)
        floor = checkpoint.offset if thread.runtime.read_key == checkpoint.session_file else 0
        try:
            final, user = reader.publication_input(
                after=floor, through=revision.size,
                files=NativeSessionFiles.for_identity(NativeEvidenceScope.locate_session(str(path)),
                                                      root=self.bus.bus.path.parent, owner=incarnation),
            )
        except MalformedRecord as error:
            raise RelationViolationError("Native publication evidence is invalid") from error
        ids = routing.annotation_ids(reader, checkpoint, thread.runtime, final.end)
        inputs = InputDispositions(self.bus.bus.path.parent)
        lookup = incarnation.lookup
        with self.bus.bus.reading() as source, self.registry.store.current_snapshot() as cut:
            snapshot = cut.snapshot
            incarnation = lease.identity.incarnation.resolved(snapshot)
            current = RegistryOwner.capture_local(snapshot, incarnation.name).thread
            if current.turn_lease != lease:
                raise RelationViolationError("Native publication turn lease changed")
            if (
                current.runtime.read_key != thread.runtime.read_key
                or FileRevision.capture(path) != revision
            ):
                raise RelationViolationError("Native publication session changed")
            if not inputs.in_turn(lease.turn_id).started_for_native(
                lease, user.require_input_id(), user.message.text, snapshot=snapshot
            ):
                raise RelationViolationError("Native publication has no original STARTED input")
            references = tuple(dict.fromkeys((*routing.requests, *published)))
            originals = (
                source.conversation(
                    lookup,
                    sequences=tuple(ref.seq for ref in references),
                    limit=len(references),
                    descending=False,
                )
                if references
                else ()
            )
            if {item.message.reference for item in originals} != set(references) or len(
                set(published)
            ) != len(published):
                raise RelationViolationError("Publication is not its original seq/id relation")
            routing.require_publications(
                tuple(item for item in originals if item.message.reference in published), lookup
            )
            self.routes.record_publication(
                thread.runtime.read_key, ids, final.entry.id, routing, published
            )
