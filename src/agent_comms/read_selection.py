"""What one local presentation captured from the registry, catalog and read documents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from .read_basis import ChannelDisplayScope, DisplayViewer, ReadCursor, UnreadQuery
from .activity_clocks import ActivityClocks
from typing import TYPE_CHECKING

from .registry_provenance import RegistryProvenance
from .registry_document import RegistrySnapshot
from .read_ledger import ReadDocument, ReadLedger
from .catalog_document import CatalogDocument
if TYPE_CHECKING:
    from .bus import BusSnapshot
    from .catalog_store import ChannelCatalog
    from .channels import Channels
    from .registration import Registration


@dataclass(frozen=True, slots=True)
class DisplayUnreadSelection:
    """The complete captured inputs that determine an unread count."""

    seen: ReadCursor
    scopes: tuple[ChannelDisplayScope, ...]
    viewer_names: tuple[str, ...]
    registry: RegistryProvenance

    def queries(self) -> tuple[UnreadQuery, ...]:
        """Each scope's count resolved against this registry and cursor."""
        names = json.dumps(self.viewer_names)
        return tuple(
            UnreadQuery(
                scope.channel,
                ReadCursor.rows(self.registry, {"viewer": self.seen.counted(scope, self.registry)}),
                self.seen.lower_bound(scope, self.registry),
                *scope.index_predicate,
                names,
            )
            for scope in self.scopes
        )


@dataclass(frozen=True, slots=True)
class CapturedSelection:
    """One captured registry/catalog/read basis for local human presentation."""

    registry: RegistrySnapshot
    catalog: CatalogDocument
    activity: ActivityClocks
    viewer: DisplayViewer
    reads: ReadLedger
    document: ReadDocument

    def seen(self, source: BusSnapshot) -> ReadCursor:
        return self.viewer.cursor(self.reads, self.registry, self.document, source)

    def unread_selection(self, source: BusSnapshot) -> DisplayUnreadSelection:
        return DisplayUnreadSelection(
            self.seen(source), self.scopes, tuple(sorted(self.viewer_names)), self.registry,
        )
    @classmethod
    def capture(
        cls, snapshot: RegistrySnapshot, catalog: CatalogDocument,
        reads: ReadLedger, document: ReadDocument, viewer: DisplayViewer,
        activity: ActivityClocks,
    ) -> CapturedSelection:
        viewer = viewer.resolve(snapshot)
        return cls(snapshot, catalog, activity, viewer, reads, document)

    @classmethod
    def load(
        cls, registry: Registration, catalog: ChannelCatalog,
        reads: ReadLedger, viewer: DisplayViewer, activity: ActivityClocks,
    ) -> CapturedSelection:
        """Use one consistent source cut without borrowing locks for decoding."""
        snapshot, channels, read_document = registry.store.snapshot_with_pair(catalog, reads)
        return cls.capture(snapshot, channels, reads, read_document, viewer, activity)

    @property
    def channels(self) -> Channels:
        return self.catalog.views(self.activity)

    @property
    def scopes(self) -> tuple[ChannelDisplayScope, ...]:
        return tuple(
            channel.display_scope(self.registry)
            for channel in self.channels.values()
        )

    @property
    def viewer_names(self) -> frozenset[str]:
        return self.viewer.aliases(self.registry)

    def require_viewer(self) -> str:
        return self.viewer.require_name(self.registry)

    def scope(self, target: str) -> ChannelDisplayScope:
        channel = self.catalog.require_channel(target, self.activity)
        return channel.display_scope(self.registry)
