from __future__ import annotations

from abc import ABC

from agent_comms.registry.catalog_store import ChannelCatalog


class RelationshipCatalogSource(ABC):
    __slots__ = ()
    catalog: ChannelCatalog
