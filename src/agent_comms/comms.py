"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

from agent_comms.bus_context import BusContext
from agent_comms.cli_verbs.cli_command import CliBus
from agent_comms.config_options import ConfigurationCatalogs
from agent_comms.execution.context_assembly import ContextAssembly
from agent_comms.execution.input_ingress import InputIngressContext
from agent_comms.execution.input_lookup import InputLookup
from agent_comms.execution.input_text_context import InputTextContext
from agent_comms.messaging.message_notification import CapturedNotificationEvidence

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, aclosing
from functools import cached_property, partial
from pathlib import Path
from typing import ClassVar

from agent_comms.context_store import ContextStore
from agent_comms.coordination.work import CoordinationWork
from agent_comms.errors import UnregisteredThreadError
from agent_comms.execution.owner_lifecycle import OwnerLifecycle
from agent_comms.execution.transcript_sources import TranscriptSource
from agent_comms.history_views import HistoryViews
from agent_comms.input_attempt import InputAttempt
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.active_route import CommsRoute, resolve_comms_route
from agent_comms.messaging.agent_inbox import TakenInputStart
from agent_comms.messaging.channel_management import ChannelManagement
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.message_page import InboxPage
from agent_comms.messaging.message_reference import InboxStart
from agent_comms.messaging.messages import Message
from agent_comms.messaging.publisher import OwnerPublisher
from agent_comms.messaging.sending import Messaging
from agent_comms.messaging.watched import FileSource
from agent_comms.planning.goal_management import Goals
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.collaboration_ledger import CollaborationLedger
from agent_comms.registry.relationships import ThreadRelationships
from agent_comms.registry.route_client import ActivityClient
from agent_comms.registry.thread_configuration import ThreadConfiguration
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_management import ThreadManagement
from agent_comms.registry.threads import Thread
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier
from agent_comms.transcript.transcripts import Transcripts


class Comms(
    BusContext,
    CliBus,
    ConfigurationCatalogs,
    ActivityClient,
    ContextAssembly,
    InputIngressContext[Thread],
    InputTextContext[InputAttempt],
):
    input_dispositions: ClassVar[Callable[[Path], InputLookup[InputAttempt]]] = InputDispositions
    messaging: Messaging

    def __init__(self, root: CommsRoute) -> None:
        super().__init__(root)
        self.goals = Goals(self.root, self.registry, self.bus, self.agents)
        self.messaging = Messaging(self.root, self.registry, self.bus, self.goals)
        self.ledger = CollaborationLedger.at(self.root, self.registry)
        self.contexts = ContextStore(self.root)
        self.owners = OwnerLifecycle(self.root, self.registry, self.bus, self.goals, self.messaging)
        self.transcripts = Transcripts(self.root, self.registry, self.bus)
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
        self.relationships = ThreadRelationships(self.views)

    def notification_evidence(self) -> CapturedNotificationEvidence:
        reads = self.bus.reads
        return CapturedNotificationEvidence(
            self.root, reads, reads.read(), self.registry.snapshot())

    async def watch(
        self, name: str, *, after: InboxStart = TakenInputStart(),
    ) -> AsyncGenerator[Message]:
        """Wait unlocked; each inbox read owns one client call on a worker, so the caller's loop
        keeps serving, and streaming records nothing."""
        reader = await CoordinationWork.run_worker(partial(self.watched_reader, name))
        maintenance = MaintenanceBarrier(self.registry.store.path)
        bus = self.bus.bus
        sources = (*bus.commit_sources(), FileSource(maintenance.state_path))
        async with aclosing(bus.watch_sources(*sources)) as changes:
            async for _ in changes:
                while True:
                    batch = await CoordinationWork.run_worker(
                        partial(self.watched_batch, name, reader, after))
                    for message in batch.page.messages:
                        yield message
                        after = after.advanced(message.reference)
                    if not batch.remaining:
                        break

    @cached_property
    def bus(self) -> MessageBus:
        return MessageBus(self.root, self.registry, OwnerPublisher)

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
