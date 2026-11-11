from __future__ import annotations

from dataclasses import dataclass

from agent_comms.messaging.activity_clocks import ActivityClocks
from agent_comms.messaging.bus import BusSnapshot
from agent_comms.messaging.channels import Channels
from agent_comms.messaging.display_unread_selection import DisplayUnreadSelection
from agent_comms.messaging.read_basis import (
    ChannelDisplayScope,
    DisplayViewer,
    ReadCursor,
)
from agent_comms.messaging.read_ledger import ReadDocument, ReadLedger
from agent_comms.registry.catalog_document import CatalogDocument
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegistrySnapshot


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
