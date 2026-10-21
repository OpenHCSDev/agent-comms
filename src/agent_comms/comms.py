"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from .active_route import CommsRoute, resolve_comms_route
from .agent_activity import AgentActivity
from .bus_route_counts import CompletingRouteCounts, OpenedRouteCounts
from .catalog_store import ChannelCatalog
from .channel_management import ChannelManagement
from .collaboration_ledger import CollaborationLedger
from .context_store import ContextStore
from .goal_management import Goals
from .history_views import HistoryViews
from .launch_runtime import LaunchRuntime
from .root_runtime import RootRuntime
from .locked_store import RegistryFile
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .messaging import Messaging
from .owner_lifecycle import OwnerLifecycle
from .publisher import OwnerPublisher, Publisher
from .registration import Registration
from .relationships import ThreadRelationships
from .thread_configuration import ThreadConfiguration
from .thread_management import ThreadManagement
from .transcripts import Transcripts
from .wire_log import WireLog


class Comms:
    def __init__(
        self, root: CommsRoute | Path, publisher: type[Publisher] = OwnerPublisher, *,
        projection: type[OpenedRouteCounts] = OpenedRouteCounts,
    ) -> None:
        self.route = resolve_comms_route(root)
        RootRuntime.require_attachable(self.route.root, LaunchRuntime.running())
        self.route.observe_root()
        publisher.prepare_root(self.root)
        self._wire_lock_path = self.root / "wire"
        self.registry = Registration(RegistryFile.path_in(self.root))
        self.bus = MessageBus(
            WireLog.path_in(self.root),
            self.registry,
            publisher,
            projection=projection,
        )
        self.messaging = Messaging(self.root, self.registry, self.bus)
        self.agents = AgentActivity(self.root, self.registry)
        self.ledger = CollaborationLedger.at(self.root, self.registry)
        self.contexts = ContextStore(self.root)
        self.goals = Goals(self.root, self.registry, self.bus, self.agents)
        self.owners = OwnerLifecycle(self.root, self.registry, self.bus, self.goals, self.messaging)
        self.transcripts = Transcripts(self.root, self.registry, self.bus, self.messaging)
        self.threads = ThreadManagement(
            self.root, self.registry, self.bus,
            ChannelCatalog.at(self.root), self.agents, self.owners, self.ledger
        )
        self.configuration = ThreadConfiguration(self.root, self.registry, self.threads, self.agents)
        self.channels = ChannelManagement(self.root, self.registry, self.bus, self.threads)
        self.views = HistoryViews(
            self.root,
            self.registry,
            self.bus,
            self.channels,
            self.agents,
            self.transcripts,
            self.messaging,
            self.goals,
            self.ledger,
        )
        self.relationships = ThreadRelationships(self.root, self.registry, self.bus, self.views)

    @classmethod
    def blocking_reads(cls, root: CommsRoute | Path) -> Self:
        """Non-UI callers complete their indexed reads instead of yielding preparation."""
        return cls(root, projection=CompletingRouteCounts)

    @property
    def root(self) -> Path:
        return self.route.root


def wire(
    root: CommsRoute | Path | str | None = None, *, projection: type[OpenedRouteCounts] = OpenedRouteCounts
) -> Comms:
    """Build a Comms wire from an explicit root or the active default route."""
    return Comms(resolve_comms_route(root), projection=projection)


@contextmanager
def client_wire(root: CommsRoute | Path) -> Iterator[Comms]:
    """One client call: it borrows the published route until it retires and answers in full.

    Only the UI yields between index units.
    """
    route = resolve_comms_route(root)
    # Refuse before borrowing a route or opening stores held by maintenance.
    # This grants nothing: writes still check under their determining locks.
    MaintenanceBarrier(RegistryFile.path_in(route.root)).assert_open_unlocked()
    with route.hold_client_route():
        yield Comms.blocking_reads(route)
