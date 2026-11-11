"""Live publication and reads, independent of historical presentation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_comms.messaging.activity_clocks import ActivityClocks
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.channel_targets import ChannelTarget
from agent_comms.messaging.channels import Channel
from agent_comms.messaging.display_sources import ActivitySource
from agent_comms.messaging.read_ledger import ReadLedger
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.catalog_store import ChannelCatalog


class BusResources(ActivitySource):
    """The original live bus, read ledger and shared activity read."""
    __slots__ = ()

    bus: Bus
    reads: ReadLedger

    @contextmanager
    def opened_activity(self) -> Iterator[ActivityClocks]:
        yield self.bus.activity_clocks()


class PublishedBus[Writes](BusResources, ABC):
    """The live resources; historical clients add their own independent data."""

    @abstractmethod
    def __init__[Registry](self, root: Path, registry: Registry, publisher: type[Writes]) -> None: ...

    publisher: Writes
    _channels: ChannelCatalog


    def require_channel(self, target: str) -> Channel:
        return self.channel_in(self._channels.read(), target)

    def channel_in(self, catalog: CatalogAccess, target: str) -> Channel:
        """A lookup needs its own channel's clocks only. Every inbox read makes several, and the
        whole index is a thousand rows."""
        name = ChannelTarget.normalize_name(target)
        return catalog.require_channel(name, self.bus.channel_clocks(name))

    def channel_declarations(self):
        with self.opened_activity() as activity:
            return self._channels.read().views(activity)
