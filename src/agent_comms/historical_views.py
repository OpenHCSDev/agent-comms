"""Source-bound historical display values. These carry no delivery authority."""

from __future__ import annotations
from agent_comms.repo import Checkout

import shutil
import tempfile
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .bus import Bus
from .child_process import Platform
from .catalog_store import ChannelCatalog
from .channel_targets import NamedTarget, ThreadTarget
from .channels import Channel
from .coordination_errors import HistoricalSourceChanged
from .errors import UnregisteredThreadError
from .field_codec import FieldCodec, ScratchName
from .locked_store import DocumentChange, RegistryFile, RootFile
from .maintenance_barrier import CarriedDocument
from .message_page import (
    NEWEST_PAGE,
    ConversationMessagePageRequest,
    MessagePage,
    MessagePageRequest,
    PageBounds,
)
from .messages import CarriesEnvelope, Message
from .page_traversal import HistoryCursor, OlderTraversal
from .presentation import MessageNotification, OutcomeViewer
from .private_path import FileObservation, FileRevision, ObservedPath, PrivateDirectoryRole
from .read_basis import (
    AnonymousViewer,
    ChannelDisplayIdentity,
    ChannelDisplayScope,
    DisplayBasis,
    DisplayIdentity,
    DisplayViewer,
    DMDisplayBasis,
    DMDisplayIdentity,
    DMDisplayScope,
    MessageDisplayScope,
    NamedViewer,
    NoDisplaySelection,
)
from .read_ledger import ReadLedger
from .recorded_bus import RecordedBusIdentity
from .registration import Registration
from .registry_document import RegistrySnapshot
from .registry_provenance import RegistryProvenance
from .revision_views import BusSourceView
from .store_files import SingleFileStore, SourceTier, StoreOwner
from .thread_identity import UnrecordedThreadIncarnation
from .threads import ThreadProvenance
from .transcript_routes import TranscriptRoutes

if TYPE_CHECKING:
    from .catalog_document import CatalogDocument
    from .history_views import HistoryViews
    from .presentation import BusPresentation, WireRevision
    from .threads import Thread


class HistoryView(ABC):
    """A requested view binds its predicate once to each original registry."""

    @property
    def viewer(self) -> DisplayViewer:
        return AnonymousViewer()

    def live_page(self, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        """Bind the query to the bus owner's snapshot and read that exact source."""
        with presentation.snapshot(self.viewer) as (basis, source):
            request = self.page_request(self.capture(basis.registry), bounds)
            return request.read_opened(source)

    @contextmanager
    def reading(self, presentation: BusPresentation) -> Iterator[Callable[[PageBounds], MessagePage]]:
        """Borrow one cut for the first tail, subsequent pages and scroll-back.

        The caller retains this lifetime only while its conversation revision is
        unchanged, and retires it off the UI thread when that source is replaced.
        """
        with presentation.reading(self.viewer) as captured:
            history = captured.bus.history.captured()
            yield partial(history.integrated_page, self, captured)

    def page(self, presentation: BusPresentation, bounds: PageBounds = NEWEST_PAGE) -> MessagePage:
        with self.reading(presentation) as read:
            return read(bounds)

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
    def revision(cls, views: HistoryViews, target: NamedTarget,
                 earlier: ConversationObservation,
                 *, worktree: Checkout) -> ConversationRevision:
        return earlier.capture(
            views.revision(), partial(cls._read_revision, views, target, worktree))

    @classmethod
    def _read_revision(cls, views: HistoryViews, target: NamedTarget,
                       worktree: Checkout) -> ConversationRevision:
        viewer = views.messaging.user_identity(worktree)
        document, catalog, stores = views.registry.store.read_pair_observed(
            views.channels.catalog, views.revision)
        snapshot = RegistrySnapshot.capture(document)
        return ConversationRevision(
            stores, cls._selection(
                views, snapshot, catalog,
                snapshot.require(viewer.name), target, worktree,
                FileRevision.capture(views.bus.history.path),
            ),
        )

    @classmethod
    @abstractmethod
    def _selection(cls, views: HistoryViews, snapshot: RegistrySnapshot, catalog: CatalogDocument,
                   viewer: Thread, target: NamedTarget, worktree: Checkout,
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
                   viewer: Thread, target: NamedTarget, worktree: Checkout,
                   history_revision: FileObservation) -> ChannelDisplayIdentity:
        with views.bus.opened_activity() as activity:
            channel = catalog.require_channel(target.selected_name(snapshot), activity)
        return ChannelDisplayIdentity(
            viewer=viewer.incarnation,
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
    second: ThreadTarget

    def capture(self, snapshot: RegistryProvenance) -> DMDisplayScope:
        return DMDisplayScope.capture(
            self.first, self.second.selected_name(snapshot), snapshot)


@dataclass(frozen=True, slots=True)
class DMDisplayHistory(ConversationHistoryView, DMHistory):
    worktree: Checkout

    @classmethod
    def _selection(cls, views: HistoryViews, snapshot: RegistrySnapshot, catalog: CatalogDocument,
                   viewer: Thread, target: NamedTarget, worktree: Checkout,
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
            project_path=worktree.resolved().path, requested_peer=target.name,
            viewer=viewer.incarnation, peer=peer,
            scope=DMDisplayScope.capture(viewer.name, name, snapshot), history_revision=history_revision,
        )

    @property
    def viewer(self) -> NamedViewer:
        return NamedViewer(self.first)

    def live_page(self, presentation: BusPresentation, bounds: PageBounds) -> MessagePage:
        bus = presentation.bus
        with presentation.snapshot(self.viewer) as (basis, source):
            snapshot = basis.registry
            peer = self.second.selected_name(snapshot)
            missing = peer not in snapshot.threads
            if missing and bus.history.threads(peer):
                return MessagePage((), False, False, NoDisplaySelection())
            return DMDisplayBasis.fetch_captured(
                bus, presentation.registry, bus.bus.path.parent,
                self.second, basis, source, worktree=self.worktree, bounds=bounds,
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

    def __post_init__(self) -> None:
        # Archived transcripts stay with the original root; the snapshot copies no sessions.
        object.__setattr__(self, "provenance", self.provenance.bound_to(Path(self.original_root)))

    @property
    def key(self) -> str:
        return self.root

    @classmethod
    def capture_original(cls, root: Path) -> HistorySource:
        """The original namespace and file revision come from the same retained document."""
        captured = Registration(RegistryFile.path_in(root)).store.read_revision()
        identity = RecordedBusIdentity.capture(Bus.path_in(root))
        return cls(
            str(root), str(root), identity, identity, Bus(Bus.path_in(root)).revision(),
            captured.source.revision, RegistryProvenance.capture(captured.document),
        )

    @staticmethod
    def matching_original(sources: tuple[HistorySource, ...], root: Path) -> Iterator[HistorySource]:
        """Every archive of this original, in the catalog's declared order."""
        return (source for source in sources if source.original_root == str(root))

    def retain(self, destination: Path) -> HistorySource:
        """Copy this original into an unpublished directory without changing its inputs."""
        root = Path(self.original_root)
        self.require_original(root)
        catalog_path = ChannelCatalog.path_in(root)
        catalog_revision = FileRevision.capture(catalog_path)
        archived = Bus(Bus.path_in(destination))
        Bus(Bus.path_in(root)).backup(archived.path)
        for path in (RegistryFile.path_in(root), catalog_path):
            if path.exists():
                target = destination / path.name
                shutil.copy2(path, target)
                Platform.current().sync_file(target)
        TranscriptRoutes(root).retain(destination)
        Registration(RegistryFile.path_in(destination)).store.mark_historical()
        self.require_original(root)
        if FileRevision.capture(catalog_path) != catalog_revision:
            raise ValueError("Historical catalog changed during snapshot; retry")
        if archived.revision() != self.snapshot_bus_revision:
            raise ValueError("Historical bus changed during snapshot; retry")
        if not self.provenance.threads:
            raise ValueError("Historical source has no identity declarations")
        Platform.current().sync_directory(destination)
        return HistorySource(
            str(destination.resolve()), self.original_root, self.bus_identity,
            RecordedBusIdentity.capture(archived.path), self.snapshot_bus_revision,
            FileRevision.capture(RegistryFile.path_in(destination)), self.provenance,
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

    def notification_references(self, sources: tuple[HistorySource, ...], references,
                                viewer: OutcomeViewer):
        """Read this namespace within its archive's current catalogue read, as this viewer
        may see it."""

        if self.require(sources, self.key) != self:
            raise HistoricalSourceChanged("Historical source changed; refresh history")
        root = Path(self.root)
        result = MessageNotification.read_references(
            Bus(Bus.path_in(root)), references,
            partial(MessageNotification.recorded_delivery_window, root, self.provenance,
                    viewer=viewer),
        )
        self.validate()
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalMessage(CarriesEnvelope, Message):
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

        return cls.from_message(
            message,
            role=message.role.as_historical(),
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


@dataclass(frozen=True, slots=True)
class HistoricalThread:
    source: HistorySource
    thread: ThreadProvenance


class HistoryArchive(SingleFileStore, CarriedDocument[tuple[HistorySource, ...]], SourceTier):
    """Ordered immutable source snapshots; never a delivery/launch authority."""

    filename = "history_sources.json"

    record_type = tuple[HistorySource, ...]

    def empty(self) -> tuple[HistorySource, ...]:
        return ()

    @classmethod
    def files_in(cls, root: Path) -> frozenset[Path]:
        """Archived roots are found by where attach puts them, never by decoding the
        catalogue: a copy of a root in an older shape must copy before any carry reads it."""
        declared = super().files_in(root)
        for archived in cls.archived_roots(root):
            declared |= RootFile.paths_in(archived)
        return declared

    @staticmethod
    def archive_directory(root: Path) -> Path:
        return root / "history"

    @classmethod
    def archived_roots(cls, directory: Path) -> tuple[Path, ...]:
        archives = cls.archive_directory(directory)
        if not archives.is_dir():
            return ()
        return tuple(sorted(path for path in archives.iterdir()
                            if path.is_dir() and not StoreOwner.is_staging(path)))

    def sources(self) -> tuple[HistorySource, ...]:
        with self.read_sources() as sources:
            return sources

    def captured(self) -> CapturedHistoryArchive:
        """Retain the catalogue and its revision from the same original descriptor."""
        document = self._capture_unlocked()
        return CapturedHistoryArchive(
            self.path, self.read_copied(document), document.revision,
        )

    @property
    def history_revision(self) -> FileObservation:
        return self.captured().catalogue_revision

    @contextmanager
    def reading(self) -> Iterator[CapturedHistoryArchive]:
        """Validate a page's original catalogue before and after using it."""
        captured = self.captured()
        self._require_revision(captured.catalogue_revision)
        yield captured
        self._require_revision(captured.catalogue_revision)

    @contextmanager
    def read_sources(self) -> Iterator[tuple[HistorySource, ...]]:
        with self.reading() as captured:
            yield captured.sources()

    def _require_revision(self, revision: FileObservation) -> None:
        if FileRevision.capture(self.path) != revision:
            raise HistoricalSourceChanged("Historical catalogue changed; refresh history")

    def attach(
        self, source_root: Path, *, source_read: HistorySource | None = None,
    ) -> HistorySource:
        """Prepare a source without archive exclusion, then publish it exactly once."""
        source_root = source_root.resolve()
        if source_root == self.path.parent.resolve():
            raise ValueError("The live bus cannot be its own history source")
        for existing in HistorySource.matching_original(self.sources(), source_root):
            return existing
        parent = self.archive_directory(self.path.parent)
        PrivateDirectoryRole.make_parents(parent)
        stage = Path(tempfile.mkdtemp(
            prefix=StoreOwner.staging_name.format(name="source", token=""),
            suffix=StoreOwner.staging_suffix, dir=parent))
        try:
            original = HistorySource.capture_original(source_root) if source_read is None else source_read
            original.require_original(source_root)
            catalog_revision = FileRevision.capture(ChannelCatalog.path_in(source_root))
            prepared = original.retain(stage)
            target = parent / f"source-{ScratchName.mint()}"

            def publish(sources: tuple[HistorySource, ...]) -> DocumentChange[tuple[HistorySource, ...], HistorySource]:
                for existing in HistorySource.matching_original(sources, source_root):
                    return DocumentChange(sources, existing)
                original.require_original(source_root)
                if FileRevision.capture(ChannelCatalog.path_in(source_root)) != catalog_revision:
                    raise ValueError("Historical catalog changed before publication; retry")
                PrivateDirectoryRole.make_parents(parent)
                stage.rename(target)
                Platform.current().sync_directory(parent)
                Platform.current().sync_directory(self.path.parent)
                source = replace(prepared, root=str(target.resolve()))
                return DocumentChange((*sources, source), source)

            return self.update(publish)
        finally:
            # A promoted directory may already be published; only the private stage is ours to discard.
            if stage.exists():
                shutil.rmtree(stage)

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
            with presentation.snapshot(view.viewer) as (basis, _):
                opened = Bus(Bus.path_in(Path(source.root)))
                raw = request.read_opened(opened)
                projected = replace(raw, messages=tuple(
                    HistoricalMessage.project(message, source, index, snapshot)
                    for message in raw.messages))
                page = replace(projected, selection=view.viewer.historical_selection(
                    projected, presentation, source, opened, basis.document, basis.registry))
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
        with self.reading() as captured:
            return captured._integrated_page(view, presentation, bounds)

    def _integrated_page(
        self, view: HistoryView, presentation: BusPresentation, bounds: PageBounds
    ) -> MessagePage:
        page = self._source_page(view, presentation, bounds)
        pages = deque((page,))
        while (following := bounds.after(page)) is not None:
            part = self._source_page(view, presentation, following)
            if not bounds.fits(len(page.messages) + len(part.messages)):
                break
            bounds.traversal.append(pages, part)
            page = MessagePage.combine(tuple(pages))
        return page

    def _source_page(
        self, view: HistoryView, presentation: BusPresentation, bounds: PageBounds
    ) -> MessagePage:
        page = self._select_source_page(view, presentation, bounds)
        return replace(page, selection=replace(
            page.selection, history_revision=self.history_revision,
        ))

    def _select_source_page(
        self, view: HistoryView, presentation: BusPresentation, bounds: PageBounds
    ) -> MessagePage:
        historical = bounds.traversal.historical
        def historical_page() -> MessagePage:
            history = self.page(
                view, presentation, bounds if historical else replace(bounds, traversal=OlderTraversal())
            )
            if history.messages:
                latest = view.live_page(presentation, PageBounds(limit=1))
                return replace(
                    history,
                    has_newer=history.has_newer or bool(latest.messages),
                )
            selected = history if historical else page
            if historical:
                return bounds.traversal.after_exhausted_history(
                    lambda traversal: view.live_page(presentation, replace(bounds, traversal=traversal)),
                    selected,
                )
            return selected

        if historical:
            return historical_page()
        page = view.live_page(presentation, bounds)
        return bounds.traversal.source_page(
            replace(page, has_older=page.has_older or bool(self.sources())),
            historical_page,
        )

    def threads(self, name: str | None = None) -> tuple[HistoricalThread, ...]:
        result: list[HistoricalThread] = []
        for source in self.sources():
            source.validate()
            result.extend(HistoricalThread(source, thread)
                          for thread in source.provenance.threads.values()
                          if name is None or thread.incarnation.matches_recorded_name(name, source.provenance))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class CapturedHistoryArchive(HistoryArchive, declared_name=None):
    """One reading's immutable catalogue; page logic remains on HistoryArchive."""

    captured_sources: tuple[HistorySource, ...]
    catalogue_revision: FileObservation

    def captured(self) -> CapturedHistoryArchive:
        return self

    def sources(self) -> tuple[HistorySource, ...]:
        return self.captured_sources
