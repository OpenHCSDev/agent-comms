"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, aclosing, contextmanager
from functools import partial
from pathlib import Path
from typing import ClassVar

from .active_route import CommsRoute, resolve_comms_route
from .agent_activity import AgentActivity
from .catalog_store import ChannelCatalog
from .channel_management import ChannelManagement
from .collaboration_ledger import CollaborationLedger
from .context_store import ContextStore
from .coordinator import Coordination
from .errors import UnregisteredThreadError
from .goal_management import Goals
from .history_views import HistoryViews
from .launch_runtime import LaunchRuntime
from .locked_store import RegistryFile
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .message_reference import FirstUnreadStart, InboxStart
from .messages import Message
from .messaging import Messaging
from .owner_lifecycle import OwnerLifecycle
from .publisher import OwnerPublisher, Publisher
from .registration import Registration
from .relationships import ThreadRelationships
from .read_basis import WatchAcknowledgment
from .root_runtime import RootRuntime
from .thread_configuration import ThreadConfiguration
from .thread_management import ThreadManagement
from .transcripts import Transcripts
from .watched import FileSource


class Comms:
    default_publisher: ClassVar[type[Publisher]] = OwnerPublisher

    def __init__(
        self, root: CommsRoute, publisher: type[Publisher] = default_publisher,
    ) -> None:
        self.route = root
        RootRuntime.require_attachable(self.route.root, LaunchRuntime.running())
        self.route.observe_root()
        self.registry = Registration(RegistryFile.path_in(self.root))
        self.bus = MessageBus(
            self.root,
            self.registry,
            publisher,
        )
        self.agents = AgentActivity(self.root, self.registry)
        self.goals = Goals(self.root, self.registry, self.bus, self.agents)
        self.messaging = Messaging(self.root, self.registry, self.bus, self.goals)
        self.ledger = CollaborationLedger.at(self.root, self.registry)
        self.contexts = ContextStore(self.root)
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

    @property
    def root(self) -> Path:
        return self.route.root

    @classmethod
    @contextmanager
    def client(cls, route: CommsRoute) -> Iterator[Comms]:
        """One complete client call on this selected route."""
        # Refuse before opening stores held by maintenance. This grants nothing:
        # each write checks again inside its own store's write transaction.
        MaintenanceBarrier(RegistryFile.path_in(route.root)).assert_open_unlocked()
        yield cls(route)

    async def watch(
        self, name: str, *, acknowledgment: type[WatchAcknowledgment],
        after: InboxStart = FirstUnreadStart(),
    ) -> AsyncIterator[Message]:
        """Wait unlocked; each inbox read and acknowledgment owns one client call."""
        with Comms.client(self.route) as comms:
            reader = comms.registry.require(name).incarnation
        maintenance = MaintenanceBarrier(self.registry.store.path)
        async with aclosing(self.bus.bus.watch(FileSource(maintenance.state_path))) as changes:
            async for _ in changes:
                while True:
                    with Comms.client(self.route) as comms:
                        snapshot = comms.registry.snapshot()
                        batch = await Coordination.run_worker(partial(
                            comms.bus.inbox_at, name, snapshot, after=after,
                        ))
                        if not reader.current(comms.registry.snapshot()):
                            raise UnregisteredThreadError("Watched thread was removed or replaced")
                    for message in batch.page.messages:
                        yield message
                        with Comms.client(self.route) as comms:
                            acknowledgment.taken(comms.bus.reads, batch.displayed.through(message.seq))
                        after = after.advanced(message.reference)
                    if not batch.remaining:
                        break


def wire(
    root: Path | str | None = None,
    publisher: type[Publisher] = Comms.default_publisher,
) -> Comms:
    """Build a Comms wire from an explicit root or the active default route."""
    route = resolve_comms_route(root)
    return Comms(route, publisher)


def client_wire(root: Path | str | None = None) -> AbstractContextManager[Comms]:
    """One client call: it borrows the published route until it retires and answers in full.

    Every reader observes committed bus rows without preparing another store.
    """
    return Comms.client(resolve_comms_route(root))
