"""Registry-bound publication and current reads, independent of historical display."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .activity_clocks import ActivityClocks
from .bus import Bus, BusSnapshot
from .catalog_document import CatalogAccess
from .catalog_store import ChannelCatalog
from .channel_targets import ChannelTarget
from .channels import Channel
from .publisher import Publisher
from .read_basis import AnonymousViewer, DisplayViewer
from .read_ledger import ReadLedger
from .read_selection import CapturedSelection
from .registry.registration import Registration


class PublishedBus[Writes: Publisher]:
    """Own the bus, publication and live read resources without a history archive."""

    def __init__(self, root: Path, registry: Registration, publisher: type[Writes]):
        self.bus = Bus(Bus.path_in(root))
        self.reads = ReadLedger.at(root)
        self._registry = registry
        self._channels = ChannelCatalog.at(root)
        self.publisher = publisher(self.bus, registry, self._channels, self.opened_activity)

    @contextmanager
    def opened_activity(self) -> Iterator[ActivityClocks]:
        yield self.bus.activity_clocks()

    def require_channel(self, target: str) -> Channel:
        return self.channel_in(self._channels.read(), target)

    def channel_in(self, catalog: CatalogAccess, target: str) -> Channel:
        """Read only the requested channel's clocks, not the entire channel index."""
        name = ChannelTarget.normalize_name(target)
        return catalog.require_channel(name, self.bus.channel_clocks(name))

    def channel_declarations(self):
        with self.opened_activity() as activity:
            return self._channels.read().views(activity)

    @contextmanager
    def snapshot(
        self, viewer: DisplayViewer = AnonymousViewer(),
    ) -> Iterator[tuple[CapturedSelection, BusSnapshot]]:
        with self.bus.reading() as source:
            basis = CapturedSelection.load(
                self._registry, self._channels, self.reads, viewer, source.activity_clocks(),
            )
            yield basis, source
