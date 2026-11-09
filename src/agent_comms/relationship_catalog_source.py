from __future__ import annotations

from abc import ABC

from agent_comms.catalog_store import ChannelCatalog


class RelationshipCatalogSource(ABC):
    __slots__ = ()
    catalog: ChannelCatalog
