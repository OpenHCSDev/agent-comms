"""Source-bound historical display values. These carry no delivery authority."""

from __future__ import annotations
from agent_comms.repo import Checkout

import shutil
import tempfile
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterator, Sequence
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
from .message_reference import MessageReference
from .messages import CarriesEnvelope, Message
from .notification_evidence import NotificationWindow
from .page_traversal import HistoryCursor, OlderTraversal
from .presentation import MessageNotification, OutcomeViewer, FirstRecipient, RecipientCursor
from .notification_evidence import RecipientSelection
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
        snapshot = presentation.registry.snapshot()
        peer = self.second.selected_name(snapshot)
        missing = peer not in snapshot.threads
        if missing and bus.history.threads(peer):
            return MessagePage((), False, False, NoDisplaySelection())
        return DMDisplayBasis.fetch_page(
            bus,
            presentation.registry,
            bus.bus.path.parent,
            snapshot.require(self.first),
            self.second,
            worktree=self.worktree, bounds=bounds,
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

    def notification_references(self, sources: tuple[HistorySource, ...], references: Sequence[MessageReference],
                                viewer: OutcomeViewer, *, after: RecipientCursor = FirstRecipient(),
                                limit: int = RecipientSelection.max_limit) -> NotificationWindow:
