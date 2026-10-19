"""Source-bound historical display values. These carry no delivery authority."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .locked_store import RootFile
from .coordination_errors import HistoricalSourceChanged
from .channels import Channel
from .errors import UnregisteredThreadError
from .field_codec import FieldCodec
from .message_page import (
    ConversationMessagePageRequest,
    MessagePage,
    NEWEST_PAGE,
    MessagePageRequest,
    PageBounds,
)
from .page_traversal import HistoryCursor, NewerTraversal, OlderTraversal
from .messages import Message
from .private_path import FileIdentity, FileRevision, PresentPath
from .private_registry_guard import PrivateRegistryGuard
from .read_basis import (
    AnonymousViewer, ChannelDisplayScope, ChannelDisplayIdentity, DisplayBasis, DisplayIdentity, DisplayViewer,
    DMDisplayScope, DMDisplayIdentity, MessageDisplayScope, NamedViewer, NoDisplaySelection,
)
from .registration import Registration
from .read_ledger import ReadLedger
from .recorded_bus import RecordedBusIdentity
from .registry_provenance import RegistryProvenance
from .store_files import _atomic_write_text, _iter_jsonl_records, _store_lock
from .thread_identity import ThreadProvenance, UnrecordedThreadIncarnation
from .revision_views import BusSourceView
from .wire_log import WireLog
from .wire_metadata import ArchivedAccess

if TYPE_CHECKING:
    from .presentation import BusPresentation, WireRevision
    from .history_views import HistoryViews
    from .registry_document import RegistrySnapshot
    from .catalog_document import CatalogDocument


class HistoryView(ABC):
    """A requested view binds its predicate once to each original registry."""

    viewer: DisplayViewer = AnonymousViewer()

    def live_page(self, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        """Bind the query to the bus owner's snapshot and read that exact source."""
        with presentation.bus.snapshot() as (basis, source):
            request = self.page_request(self.capture(basis.registry), bounds)
            return request.read_opened(source)

    def page(self, presentation: BusPresentation, bounds: PageBounds = NEWEST_PAGE) -> MessagePage:
        return presentation.bus.history.integrated_page(self, presentation, bounds)

    @abstractmethod
    def capture(self, snapshot: RegistryProvenance) -> MessageDisplayScope:
        """Retain the source's aliases/membership without borrowing live identity."""

    def page_request(self, scope, bounds: PageBounds) -> MessagePageRequest:
        return MessagePageRequest(scope, bounds)


@dataclass(frozen=True, slots=True)
class ConversationRevision:
    """Core's captured selection and original store observation for one view."""

    stores: WireRevision
    selection: DisplayIdentity

    def display_changed_since(self, earlier: ConversationRevision) -> bool:
        return (self.selection != earlier.selection
                or self.stores.changed_since(BusSourceView(), earlier.stores))


class ConversationHistoryView:
    """Historical views that present conversation content, not owner state."""

    @classmethod
    def same_display(cls, page: MessagePage, earlier: MessagePage) -> bool:
        """Read-scope safety uses the original pages, never a client identity copy."""
        return page.selection.identity == earlier.selection.identity

    @classmethod
    def revision(cls, views: HistoryViews, target: str, *, worktree: str) -> ConversationRevision:
        worktree = str(Path(worktree).resolve())
        viewer = views.messaging.user_identity(worktree).name
        with views.registry.store.reading(views.bus, views.bus.log, views.channels.catalog):
            snapshot = views.registry.store.read_snapshot_unlocked()
            stores = views.revision()
            return ConversationRevision(
                stores, cls._selection(
                    views, snapshot, views.channels.catalog._read_unlocked(), viewer, target, worktree, stores.history,
                ),
            )

    @classmethod
    @abstractmethod
    def _selection(cls, views: HistoryViews, snapshot: RegistrySnapshot, catalog: CatalogDocument,
                   viewer: str, target: str, worktree: str,
                   history_revision: FileRevision | None) -> DisplayIdentity: ...

    def page_request(self, scope, bounds: PageBounds) -> MessagePageRequest:
        return ConversationMessagePageRequest(scope, bounds)


@dataclass(frozen=True, slots=True)
class ChannelHistory(HistoryView):
    scope: ChannelDisplayScope

    def capture(self, snapshot: RegistryProvenance) -> ChannelDisplayScope:
        return self.scope


@dataclass(frozen=True, slots=True)
class ChannelDisplayHistory(ConversationHistoryView, HistoryView):
    channel: Channel
    viewer: DisplayViewer = AnonymousViewer()

    @classmethod
    def _selection(cls, views: HistoryViews, snapshot: RegistrySnapshot, catalog: CatalogDocument,
                   viewer: str, target: str, worktree: str,
                   history_revision: FileRevision | None) -> ChannelDisplayIdentity:
        with views.bus.opened_activity() as activity:
            channel = catalog.require_channel(target, activity)
        return ChannelDisplayIdentity(
            viewer=snapshot.require(viewer).incarnation,
            bus_identity=RecordedBusIdentity.capture(views.bus.log.path),
            scope=channel.display_scope(snapshot), history_revision=history_revision,
        )

    def live_page(self, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        return presentation.channel_page(self.channel.name, viewer=self.viewer, bounds=bounds)

    def capture(self, snapshot: RegistryProvenance) -> ChannelDisplayScope:
        return self.channel.display_scope(snapshot)


@dataclass(frozen=True, slots=True)
class DMHistory(HistoryView):
    first: str
    second: str

    def capture(self, snapshot: RegistryProvenance) -> DMDisplayScope:
        return DMDisplayScope.capture(self.first, self.second, snapshot)


@dataclass(frozen=True, slots=True)
class DMDisplayHistory(ConversationHistoryView, DMHistory):
    worktree: str

    @classmethod
    def _selection(cls, views: HistoryViews, snapshot: RegistrySnapshot, catalog: CatalogDocument,
                   viewer: str, target: str, worktree: str,
                   history_revision: FileRevision | None) -> DMDisplayIdentity:
        try:
            peer = snapshot.participant(target).incarnation
        except UnregisteredThreadError:
            peer = UnrecordedThreadIncarnation(snapshot.canonical_name(target))
        root = views.root.stat()
        return DMDisplayIdentity(
            root_identity=(root.st_dev, root.st_ino),
            bus_identity=RecordedBusIdentity.capture(views.bus.log.path),
            worktree=worktree, requested_peer=target,
            viewer=snapshot.require(viewer).incarnation, peer=peer,
            scope=DMDisplayScope.capture(viewer, target, snapshot), history_revision=history_revision,
        )

    @property
    def viewer(self) -> NamedViewer:
        return NamedViewer(self.first)

    def live_page(self, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        from .read_basis import DMDisplayBasis

        bus = presentation.bus
        if self.second not in presentation.registry and bus.history.threads(self.second):
            return MessagePage((), False, False, NoDisplaySelection())
        return DMDisplayBasis.fetch_page(
            bus,
            presentation.registry,
            bus.log.path.parent,
            self.first,
            self.second,
            worktree=self.worktree,
            bounds=bounds,
        )


@dataclass(frozen=True, slots=True)
class HistorySource:
    """An immutable bus/registry snapshot attached by the destination MessageBus."""

    root: str
    original_root: str
    wire_root_id: str
    bus_identity: RecordedBusIdentity
    size: int
    snapshot_bus_revision: FileRevision
    snapshot_registry_revision: FileRevision
    provenance: RegistryProvenance

    @property
    def key(self) -> str:
        return self.root

    def validate(self) -> None:
        root = Path(self.root)
        if (
            FileRevision.capture(root / "bus.jsonl") != self.snapshot_bus_revision
            or FileRevision.capture(root / "registry.json") != self.snapshot_registry_revision
        ):
            raise ValueError("Historical snapshot changed; restore it before browsing")

    def acknowledge(self, displayed: DisplayBasis) -> None:
        """Validate this archived source before updating its own read ledger."""
        self.validate()
        ReadLedger.at(self.root).mark_displayed(displayed.viewer, displayed)

    def require_original(self, root: Path) -> None:
        """Check an original typed acquisition before destination publication."""
        root = root.resolve()
        if self.root != str(root) or self.original_root != str(root):
            raise ValueError("Recorded provenance belongs to another original source")
        self.validate()
        log = WireLog(root / "bus.jsonl")
        info = log.path.stat()
        if (log.read_metadata_unlocked(required=True).root_id != self.wire_root_id
                or not self.bus_identity.matches(PresentPath(FileIdentity.from_stat(info)))
                or info.st_size != self.size):
            raise ValueError("Recorded provenance lost original wire ownership")

    @classmethod
    def require(cls, sources: tuple[HistorySource, ...], key: str) -> HistorySource:
        source = next((item for item in sources if item.key == key), None)
        if source is None:
            raise HistoricalSourceChanged("Historical source detached; refresh history")
        source.validate()
        return source

    def notification_references(self, sources: tuple[HistorySource, ...], references):
        """Read this namespace within its archive's current catalogue read."""
        from functools import partial

        from .presentation import MessageNotification

        if self.require(sources, self.key) != self:
            raise HistoricalSourceChanged("Historical source changed; refresh history")
        root = Path(self.root)
        result = MessageNotification.read_references(
            WireLog(root / "bus.jsonl"), references,
            partial(MessageNotification.recorded_delivery_window, root, self.provenance),
        )
        self.validate()
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalMessage(Message):
    source: HistorySource
    source_order: int
    sender_created_at: float | None
    target_created_at: float | None

    @classmethod
    def project(
        cls, message: Message, source: HistorySource, order: int, snapshot: RegistryProvenance
    ) -> HistoricalMessage:
        def creation(name: str) -> float | None:
            declaration = snapshot.threads.get(snapshot.canonical_name(name))
            # A registration created after the message cannot identify its sender
            # or recipient. The saved registry can still be inspected separately.
            return (
                declaration.created_at
                if declaration is not None and declaration.created_at <= message.timestamp
                else None
            )

        return cls(
            **{**{f.name: getattr(message, f.name) for f in fields(Message)},
               "role": message.role.as_historical()},
            source=source,
            source_order=order,
            sender_created_at=creation(message.sender),
            target_created_at=creation(message.target),
        )

    @property
    def history_source(self) -> HistorySource:
        return self.source

    @property
    def view_cursor(self) -> HistoryCursor:
        return HistoryCursor(self.source.key, self.seq)

    @property
    def view_key(self) -> tuple[str, int]:
        return self.source.key, self.seq

    @property
    def view_order(self) -> tuple[int, int, int]:
        return 0, self.source_order, self.seq

    @property
    def display_metadata(self) -> dict:
        return {
            "history": {
                "source": self.source.original_root,
                "wire_root_id": self.source.wire_root_id,
                "bus_identity": FieldCodec.encode(self.source.bus_identity),
                "sender_created_at": self.sender_created_at,
                "target_created_at": self.target_created_at,
            },
        }

    def to_wire(self) -> dict:
        # Provenance is a view, never a change to the original public message.
        return Message(**{f.name: getattr(self, f.name) for f in fields(Message)}).to_wire()


@dataclass(frozen=True, slots=True)
class HistoricalThread:
    source: HistorySource
    thread: ThreadProvenance


class HistoryArchive(RootFile):
    """Ordered immutable source snapshots; never a delivery/launch authority."""

    filename = "history_sources.json"

    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = self.path_in(bus_path.parent)

    def sources(self) -> tuple[HistorySource, ...]:
        with self.read_sources() as sources:
            return sources

    @contextmanager
    def read_sources(self) -> Iterator[tuple[HistorySource, ...]]:
        """Decode one read's catalogue; reject changes without decoding it again."""
        try:
            stream = self.path.open('rb')
        except FileNotFoundError:
            yield ()
            if self.path.exists():
                raise HistoricalSourceChanged("Historical catalogue changed; refresh history")
            return
        with stream:
            revision = FileRevision.from_file(stream)
            sources = FieldCodec.decode(tuple[HistorySource, ...], json.load(stream))
        self._require_revision(revision)
        yield sources
        self._require_revision(revision)

    def _require_revision(self, revision: FileRevision) -> None:
        if FileRevision.capture(self.path) != revision:
            raise HistoricalSourceChanged("Historical catalogue changed; refresh history")

    def attach(self, source_root: Path, *, source_read: HistorySource | None = None) -> HistorySource:
        """Snapshot a preserved source, then publish it for ordinary display.

        Only destination files are written. No Comms constructor, source locks,
        inboxes, execution inputs, or source sequence allocator are touched.
        A source is attached once. Its original bytes and identity survive.
        """
        import shutil
        import tempfile

        from .catalog_store import ChannelCatalog
        from .private_bus_checkpoint import install_private_bus_checkpoint
        from .historical_views import HistorySource
        from .transcript_routes import TranscriptRoutes

        source_root = source_root.resolve()
        if source_root == self.bus_path.parent.resolve():
            raise ValueError("The live bus cannot be its own history source")
        with _store_lock(self.path):
            sources = self.sources()
            existing = next((s for s in sources if s.original_root == str(source_root)), None)
            if existing is not None:
                return existing
            parent = self.bus_path.parent / "history"
            parent.mkdir(mode=0o700, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix="source-", dir=parent))
            try:
                paths = [
                    source_root / name
                    for name in (
                        "bus.jsonl",
                        "registry.json",
                        ChannelCatalog.filename,
                        "bus_meta.json",
                    )
                ]
                revisions = tuple(FileRevision.capture(path) for path in paths)
                if source_read is None:
                    with Registration(source_root / "registry.json").store.reading() as original:
                        provenance = RegistryProvenance.capture(original)
                else:
                    source_read.require_original(source_root)
                    provenance = source_read.provenance
                archived = WireLog(stage / "bus.jsonl")
                # Lock the empty stage before copying its marker. Once marked,
                # ordinary reads correctly require the not-yet-built index.
                with archived.locked():
                    for path in paths:
                        if path.exists():
                            shutil.copy2(path, stage / path.name)
                    TranscriptRoutes(source_root).snapshot(stage)
                    if revisions != tuple(FileRevision.capture(path) for path in paths):
                        raise ValueError("Historical source changed during snapshot; retry")
                    if source_read is not None:
                        source_read.require_original(source_root)
                    bus_info = paths[0].stat() if paths[0].exists() else None
                    if bus_info is None:
                        archived.path.touch()
                    marker = archived.read_metadata_unlocked(required=True)
                    marker.claimed_after_seq = marker.last_seq
                    marker.access = ArchivedAccess()
                    marker.checkpoint_version = None
                    marker.checkpoint_seal = None
                    guard = PrivateRegistryGuard(stage / "registry.json", marker.root_id)
                    guard.create_pending()
                    archived.write_metadata_unlocked(marker)
                    # The installer borrows this real stage lock; it never opens
                    # the incomplete archive through the ordinary read barrier.
                    install_private_bus_checkpoint(archived, _bus_locked=True)
                    guard.commit_initial()
                source = HistorySource(
                    str(stage.resolve()),
                    str(source_root),
                    marker.root_id,
                    RecordedBusIdentity.capture(source_root / "bus.jsonl"),
                    bus_info.st_size if bus_info else 0,
                    FileRevision.capture(stage / "bus.jsonl"),
                    FileRevision.capture(stage / "registry.json"),
                    provenance,
                )
                declarations = source.provenance.threads
                from .wire_log import WireSnapshot

                previous = 0
                for record, size in _iter_jsonl_records(stage / "bus.jsonl"):
                    for message, _ in WireSnapshot.public_page_records(record, size, marker):
                        if message.seq <= previous:
                            raise ValueError("Historical source has nonascending sequences")
                        previous = message.seq
                if not declarations:
                    raise ValueError("Historical source has no identity declarations")
                _atomic_write_text(
                    self.path,
                    json.dumps([FieldCodec.encode(item) for item in (*sources, source)]),
                )
                return source
            except BaseException:
                shutil.rmtree(stage)
                raise

    def page(self, view: HistoryView, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        traversal = bounds.traversal
        sources = self.sources()
        start = next(
            (i for i, source in enumerate(sources) if source.key == traversal.source),
            -1 if traversal.historical else len(sources) - 1,
        )
        if traversal.historical and start < 0:
            raise HistoricalSourceChanged("Historical source detached; reload history")
        for index in traversal.source_indexes(start, len(sources)):
            source = sources[index]
            source.validate()
            snapshot = source.provenance
            current = replace(bounds, traversal=traversal.for_source(index == start))
            request = view.page_request(view.capture(snapshot), current)
            current_snapshot = presentation.registry.snapshot()
            document = presentation.bus.reads.read()
            with WireLog(Path(source.root) / "bus.jsonl").opened_wire_snapshot() as opened:
                def read(page_index):
                    raw = request.indexed(page_index, opened)
                    projected = replace(raw, messages=tuple(
                        HistoricalMessage.project(message, source, index, snapshot)
                        for message in raw.messages))
                    return replace(projected, selection=view.viewer.historical_selection(
                        projected, presentation, source, opened, page_index, document, current_snapshot))
                page = opened.query_page_index(read)
            if page.messages:
                return replace(
                    page,
                    has_older=page.has_older or index > 0,
                    has_newer=page.has_newer or index < len(sources) - 1,
                )
        return MessagePage((), False, False, NoDisplaySelection())

    def integrated_page(
        self, view: HistoryView, presentation: BusPresentation, bounds: PageBounds
    ) -> MessagePage:
        page = self._source_page(view, presentation, bounds)
        pages = deque((page,))
        while (following := bounds.after(page)) is not None:
            part = self._source_page(view, presentation, following)
            if part.selection.history_revision != page.selection.history_revision:
                raise HistoricalSourceChanged("Historical sources changed; reload history")
            if not bounds.fits(len(page.messages) + len(part.messages),
                                 page.bytes_read + part.bytes_read):
                break
            bounds.traversal.append(pages, part)
            page = MessagePage.combine(tuple(pages))
        return page

    def _source_page(
        self, view: HistoryView, presentation: BusPresentation, bounds: PageBounds
    ) -> MessagePage:
        historical = bounds.traversal.historical
        if not self.sources() and not historical:
            return view.live_page(presentation, bounds)
        history_revision = FileRevision.capture(self.path)
        if not historical:
            page = view.live_page(presentation, bounds)
            if page.messages or isinstance(bounds.traversal, NewerTraversal):
                return replace(
                    page,
                    has_older=page.has_older or bool(self.sources()),
                    selection=replace(page.selection, history_revision=history_revision),
                )
        history = self.page(
            view, presentation, bounds if historical else replace(bounds, traversal=OlderTraversal())
        )
        if history.messages:
            latest = view.live_page(presentation, PageBounds(limit=1))
            return replace(
                history,
                selection=replace(history.selection, history_revision=history_revision),
                has_newer=history.has_newer or bool(latest.messages),
            )
        if historical and isinstance(bounds.traversal, NewerTraversal):
            live = view.live_page(presentation, replace(bounds, traversal=NewerTraversal()))
            return replace(live, selection=replace(live.selection, history_revision=history_revision))
        selected = history if historical else page
        return replace(selected, selection=replace(selected.selection, history_revision=history_revision))

    def threads(self, name: str | None = None) -> tuple[HistoricalThread, ...]:
        result = []
        for source in self.sources():
            source.validate()
            result.extend(HistoricalThread(source, thread)
                          for thread in source.provenance.threads.values()
                          if name is None or thread.incarnation.matches_recorded_name(name, source.provenance))
        return tuple(result)
