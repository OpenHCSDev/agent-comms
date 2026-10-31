"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from contextlib import AbstractContextManager, aclosing
from functools import partial
from pathlib import Path

from .active_route import CommsRoute, resolve_comms_route
from .catalog_store import ChannelCatalog
from .channel_management import ChannelManagement
from .collaboration_ledger import CollaborationLedger
from .context_store import ContextStore
from .coordinator import Coordination
from .errors import UnregisteredThreadError
from .goal_management import Goals
from .history_views import HistoryViews
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .message_reference import InboxStart
from .agent_inbox import TakenInputStart
from .messages import Message
from .messaging import Messaging
from .owner_lifecycle import OwnerLifecycle
from .publisher import OwnerPublisher
from .relationships import ThreadRelationships
from .route_client import ActivityClient
from .thread_configuration import ThreadConfiguration
from .thread_management import ThreadManagement
from .transcripts import Transcripts
from .watched import FileSource

if TYPE_CHECKING:
    from .message_page import InboxPage
    from .thread_identity import ThreadIncarnation
    from .transcript_sources import TranscriptSource


class Comms(ActivityClient):
    def __init__(self, root: CommsRoute) -> None:
        super().__init__(root)
        self.bus = MessageBus(
            self.root,
            self.registry,
            OwnerPublisher,
        )
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

    async def watch(
        self, name: str, *, after: InboxStart = TakenInputStart(),
    ) -> AsyncGenerator[Message]:
        """Wait unlocked; each inbox read owns one client call on a worker, so the caller's loop
        keeps serving, and streaming records nothing."""
        reader = await Coordination.run_worker(partial(self.watched_reader, name))
        maintenance = MaintenanceBarrier(self.registry.store.path)
        async with aclosing(self.bus.bus.watch(FileSource(maintenance.state_path))) as changes:
            async for _ in changes:
                while True:
                    batch = await Coordination.run_worker(
                        partial(self.watched_batch, name, reader, after))
                    for message in batch.page.messages:
                        yield message
                        after = after.advanced(message.reference)
                    if not batch.remaining:
                        break

    def watched_reader(self, name: str) -> ThreadIncarnation:
        with Comms.client(self.route) as comms:
            return comms.registry.require(name).incarnation

    def watched_batch(self, name: str, reader: ThreadIncarnation, after: InboxStart) -> InboxPage:
        with Comms.client(self.route) as comms:
            batch = comms.bus.inbox_at(name, comms.registry.snapshot(), after=after)
            if not reader.current(comms.registry.snapshot()):
                raise UnregisteredThreadError("Watched thread was removed or replaced")
            return batch

    def history_source(self, name: str) -> TranscriptSource:
        """The named thread's transcript source, whose requests a watch follows."""
        with Comms.client(self.route) as comms:
            thread = comms.registry.require(name)
            return thread.runtime.history_source(comms.root, thread.incarnation)


def wire(root: Path | str | None = None) -> Comms:
    """Build a Comms wire from an explicit root or the active default route."""
    route = resolve_comms_route(root)
    return Comms(route)


def client_wire(root: Path | str | None = None) -> AbstractContextManager[Comms]:
    """One client call: it borrows the published route until it retires and answers in full.

    Every reader observes committed bus rows without preparing another store.
    """
    return Comms.client(resolve_comms_route(root))
