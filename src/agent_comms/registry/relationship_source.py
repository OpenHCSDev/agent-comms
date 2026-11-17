from __future__ import annotations

from abc import abstractmethod
from pathlib import Path

from agent_comms import presentation
from agent_comms.messaging.display_sources import SentHistorySource
from agent_comms.messaging.published_bus import BusResources
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.relationship_catalog_source import RelationshipCatalogSource
from agent_comms.registry.roster_visibility import RosterVisibility


class RelationshipSource(SentHistorySource[RegistrySnapshot]):
    __slots__ = ()
    root: Path
    registry: Registration
    bus: BusResources
    channels: RelationshipCatalogSource

    @abstractmethod
    def revision(self) -> presentation.WireRevision: ...

    @abstractmethod
    def thread_views(
        self, visibility: RosterVisibility,
    ) -> tuple[presentation.ThreadView, ...]: ...
