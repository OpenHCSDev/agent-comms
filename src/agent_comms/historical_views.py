"""Source-bound historical display values. These carry no delivery authority."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .bus import Bus
from .channel_targets import CommandTarget, NamedTarget, ThreadTarget
from .channels import Channel
from .coordination_errors import HistoricalSourceChanged
from .errors import UnregisteredThreadError
from .field_codec import FieldCodec
from .locked_store import DurableFile, RegistryFile, RootFile
from .message_page import (
    NEWEST_PAGE,
    ConversationMessagePageRequest,
    MessagePage,
    MessagePageRequest,
    PageBounds,
)
from .messages import Message
from .page_traversal import HistoryCursor, NewerTraversal, OlderTraversal
from .private_path import FileObservation, FileRevision, ObservedPath
from .read_basis import (
    AnonymousViewer,
    ChannelDisplayIdentity,
    ChannelDisplayScope,
    DisplayBasis,
    DisplayIdentity,
    DisplayViewer,
    DMDisplayIdentity,
    DMDisplayScope,
    MessageDisplayScope,
    NamedViewer,
    NoDisplaySelection,
)
from .read_ledger import ReadLedger
from .recorded_bus import RecordedBusIdentity
from .registration import Registration
from .registry_provenance import RegistryProvenance
from .revision_views import BusSourceView
from .store_files import AtomicFile, _store_lock
from .thread_identity import UnrecordedThreadIncarnation
from .thread_provenance import ThreadProvenance

if TYPE_CHECKING:
    from .catalog_document import CatalogDocument
    from .history_views import HistoryViews
    from .presentation import BusPresentation, WireRevision
    from .registry_document import RegistrySnapshot


class HistoryView(ABC):
    """A requested view binds its predicate once to each original registry."""

    @property
    def viewer(self) -> DisplayViewer:
        return AnonymousViewer()

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


class ConversationObservation:
    """A first observation has no selection and therefore always reads."""

    def matches_stores(self, stores: WireRevision) -> bool:
        return False

    def capture(self, stores: WireRevision,
                read: Callable[[], ConversationRevision]) -> ConversationRevision:
        return read()

    def display_changed_before(self, later: ConversationRevision) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class ConversationRevision(ConversationObservation):
    """Core's captured selection and original store observation for one view."""

    stores: WireRevision
    selection: DisplayIdentity

    def matches_stores(self, stores: WireRevision) -> bool:
        return self.stores == stores

    def capture(self, stores: WireRevision,
                read: Callable[[], ConversationRevision]) -> ConversationRevision:
        return (type(self)(stores, self.selection) if stores.selection_reusable_from(self.stores)
                else read())

    def display_changed_since(self, earlier: ConversationObservation) -> bool:
        return earlier.display_changed_before(self)

    def display_changed_before(self, later: ConversationRevision) -> bool:
        return (self.selection != later.selection
                or later.stores.changed_since(BusSourceView(), self.stores))


class ConversationHistoryView:
    """Historical views that present conversation content, not owner state."""

    @classmethod
    def same_display(cls, page: MessagePage, earlier: MessagePage) -> bool:
        """Read-scope safety uses the original pages, never a client identity copy."""
        return page.selection.identity == earlier.selection.identity

    @classmethod
    def revision(cls, views: HistoryViews, target: str | NamedTarget,
                 earlier: ConversationObservation,
                 *, worktree: str) -> ConversationRevision:
        return earlier.capture(
            views.revision(), partial(cls._read_revision, views, target, worktree))

    @classmethod
    def _read_revision(cls, views: HistoryViews, target: str | NamedTarget,
                       worktree: str) -> ConversationRevision:
        worktree = str(Path(worktree).resolve())
        viewer = views.messaging.user_identity(worktree).name
        with views.registry.store.reading(views.channels.catalog):
            snapshot = views.registry.store.read_snapshot_unlocked()
            target = CommandTarget.named(target)
            stores = views.revision()
            return ConversationRevision(
                stores, cls._selection(
                    views, snapshot, views.channels.catalog._read_unlocked(), viewer, target, worktree,
                    FileRevision.capture(views.bus.history.path),
                ),
            )

    @classmethod
    @abstractmethod
    def _selection(cls, views: HistoryViews, snapshot: RegistrySnapshot, catalog: CatalogDocument,
                   viewer: str, target: NamedTarget, worktree: str,
                   history_revision: FileObservation) -> DisplayIdentity: ...

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
                   viewer: str, target: NamedTarget, worktree: str,
                   history_revision: FileObservation) -> ChannelDisplayIdentity:
        with views.bus.opened_activity() as activity:
            channel = catalog.require_channel(target.selected_name(snapshot), activity)
        return ChannelDisplayIdentity(
            viewer=snapshot.require(viewer).incarnation,
            bus_identity=RecordedBusIdentity.capture(views.bus.bus.path),
            scope=channel.display_scope(snapshot), history_revision=history_revision,
        )

    def live_page(self, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        return presentation.channel_page(self.channel.name, viewer=self.viewer, bounds=bounds)

    def capture(self, snapshot: RegistryProvenance) -> ChannelDisplayScope:
        return self.channel.display_scope(snapshot)


@dataclass(frozen=True, slots=True)
class DMHistory(HistoryView):
    first: str
    second: str | ThreadTarget

    def capture(self, snapshot: RegistryProvenance) -> DMDisplayScope:
        return DMDisplayScope.capture(
            self.first, CommandTarget.named(self.second).selected_name(snapshot), snapshot)


@dataclass(frozen=True, slots=True)
class DMDisplayHistory(ConversationHistoryView, DMHistory):
    worktree: str

    @classmethod
    def _selection(cls, views: HistoryViews, snapshot: RegistrySnapshot, catalog: CatalogDocument,
                   viewer: str, target: NamedTarget, worktree: str,
                   history_revision: FileObservation) -> DMDisplayIdentity:
        name = target.selected_name(snapshot)
        try:
            peer = snapshot.participant(name).incarnation
        except UnregisteredThreadError:
            peer = UnrecordedThreadIncarnation(name)
        root = views.root.stat()
        return DMDisplayIdentity(
            root_identity=(root.st_dev, root.st_ino),
            bus_identity=RecordedBusIdentity.capture(views.bus.bus.path),
            worktree=worktree, requested_peer=target.name,
            viewer=snapshot.require(viewer).incarnation, peer=peer,
            scope=DMDisplayScope.capture(viewer, name, snapshot), history_revision=history_revision,
        )

    @property
    def viewer(self) -> NamedViewer:
        return NamedViewer(self.first)

    def live_page(self, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        from .read_basis import DMDisplayBasis

        bus = presentation.bus
        target = CommandTarget.named(self.second)
        target.require_dm_peer()
        with presentation.registry.store.reading_snapshot() as snapshot:
            peer = target.selected_name(snapshot)
            missing = peer not in snapshot.threads
        if missing and bus.history.threads(peer):
            return MessagePage((), False, False, NoDisplaySelection())
        return DMDisplayBasis.fetch_page(
            bus,
            presentation.registry,
            bus.bus.path.parent,
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
    bus_identity: RecordedBusIdentity
    snapshot_bus_identity: RecordedBusIdentity
    snapshot_bus_revision: int
    snapshot_registry_revision: FileObservation
    provenance: RegistryProvenance

    @property
    def key(self) -> str:
        return self.root

    @classmethod
    def capture_original(cls, root: Path, provenance: RegistryProvenance) -> HistorySource:
        """Capture the same owned source facts used to validate a sealed archive."""
        identity = RecordedBusIdentity.capture(Bus.path_in(root))
        return cls(
            str(root), str(root), identity, identity, Bus(Bus.path_in(root)).revision(),
            FileRevision.capture(RegistryFile.path_in(root)), provenance,
        )

    def validate(self) -> None:
        root = Path(self.root)
        error = ValueError("Historical snapshot changed; restore it before browsing")
        if not self.snapshot_bus_identity.matches(ObservedPath.at(Bus.path_in(root))):
            raise error
        current = replace(
            self,
            snapshot_bus_revision=Bus(Bus.path_in(root)).revision(),
            snapshot_registry_revision=FileRevision.capture(RegistryFile.path_in(root)),
        )
        if current != self:
            raise error

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
        if not self.bus_identity.matches(ObservedPath.at(Bus.path_in(root))):
            raise ValueError("Recorded provenance lost original bus ownership")

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
            Bus(Bus.path_in(root)), references,
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
        message = message.history_snapshot

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
        return HistoryCursor(source=self.source.key, sequence=self.seq)

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


class HistoryArchive(DurableFile):
    """Ordered immutable source snapshots; never a delivery/launch authority."""

    filename = "history_sources.json"

    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = self.path_in(bus_path.parent)

    @classmethod
    def files_in(cls, root: Path) -> frozenset[Path]:
        declared = super().files_in(root)
        for source in cls(Bus.path_in(root)).sources():
            archived = Path(source.root)
            if archived.is_relative_to(root):
                declared |= RootFile.paths_in(archived)
        return declared

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
                        RegistryFile.filename,
                        ChannelCatalog.filename,
                    )
                ]
                catalog_path = ChannelCatalog.path_in(source_root)
                catalog_revision = FileRevision.capture(catalog_path)
                original_bus = Bus(Bus.path_in(source_root))
                if source_read is None:
                    with Registration(RegistryFile.path_in(source_root)).store.reading() as registry:
                        original = HistorySource.capture_original(
                            source_root, RegistryProvenance.capture(registry),
                        )
                else:
                    source_read.require_original(source_root)
                    original = source_read
                archived = Bus(Bus.path_in(stage))
                original_bus.backup(archived.path)
                for path in paths:
                    if path.exists():
                        shutil.copy2(path, stage / path.name)
                TranscriptRoutes(source_root).retain(stage)
                Registration(RegistryFile.path_in(stage)).store.mark_historical()
                original.require_original(source_root)
                error = ValueError("Historical source changed during snapshot; retry")
                if FileRevision.capture(catalog_path) != catalog_revision:
                    raise error
                if archived.revision() != original.snapshot_bus_revision:
                    raise error
                source = HistorySource(
                    str(stage.resolve()), str(source_root),
                    original.bus_identity, RecordedBusIdentity.capture(archived.path),
                    original.snapshot_bus_revision,
                    FileRevision.capture(RegistryFile.path_in(stage)), original.provenance,
                )
                if not source.provenance.threads:
                    raise ValueError("Historical source has no identity declarations")
                AtomicFile(self.path).write_text(json.dumps([FieldCodec.encode(item) for item in (*sources, source)]))
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
            opened = Bus(Bus.path_in(Path(source.root)))
            raw = request.read_opened(opened)
            projected = replace(raw, messages=tuple(
                HistoricalMessage.project(message, source, index, snapshot)
                for message in raw.messages))
            page = replace(projected, selection=view.viewer.historical_selection(
                projected, presentation, source, opened, document, current_snapshot))
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
            if not bounds.fits(len(page.messages) + len(part.messages)):
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
        result: list[HistoricalThread] = []
        for source in self.sources():
            source.validate()
            result.extend(HistoricalThread(source, thread)
                          for thread in source.provenance.threads.values()
                          if name is None or thread.incarnation.matches_recorded_name(name, source.provenance))
        return tuple(result)
