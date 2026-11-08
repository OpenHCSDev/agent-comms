"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import AbstractContextManager, aclosing
from dataclasses import dataclass
from functools import cached_property, partial
import math
import time
from typing import ClassVar
from pathlib import Path

from .active_route import CommsRoute, resolve_comms_route
from .catalog_store import ChannelCatalog
from .channel_management import ChannelManagement
from .collaboration_ledger import CollaborationLedger
from .context_store import ContextStore
from .coordination.database import CoordinationStore
from .coordination.coordinator import Coordination
from .errors import UnregisteredThreadError
from .goal_management import Goals
from .history_views import HistoryViews
from .storage.maintenance_barrier import MaintenanceBarrier
from .messaging.message_bus import MessageBus
from .messaging.message_page import InboxPage
from .messaging.message_reference import InboxStart
from .agent_inbox import TakenInputStart
from .messaging.messages import Message
from .messaging.sending import Messaging
from .execution.owner_lifecycle import OwnerLifecycle
from .publisher import OwnerPublisher
from .relationships import ThreadRelationships
from .agent_activity import AgentActivity
from .registry.registration import Registration
from .storage.private_path import FileRevision
from .registry.registry_document import RegistrySnapshot, UnreadRegistry
from .route_client import ActivityClient
from .thread_configuration import ThreadConfiguration
from .thread_identity import ThreadIncarnation
from .thread_management import ThreadManagement
from .transcript_sources import TranscriptSource
from .transcripts import Transcripts
from .watched import FileSource


class InboxActivity(AgentActivity):
    """Activity read beside the bus. A connected reader whose oldest untaken message is
    older than the stall window has stopped reading, whatever its process says: on
    2026-11-04 a py-spy that paused a session bridge left it `connected` for almost four
    minutes while its messages waited."""

    stall_seconds: ClassVar[float] = 60.0

    def __init__(self, root: Path, registry: Registration, bus: MessageBus) -> None:
        super().__init__(root, registry)
        self.bus = bus
        self.coordination = CoordinationStore.path_in(root)
        self.catalog = ChannelCatalog.at(root).path
        self.untaken = UntakenCut((), UnreadRegistry(), {})

    def stalled(self, readers: tuple[str, ...], snapshot: RegistrySnapshot) -> Mapping[str, float]:
        cutoff = time.time() - self.stall_seconds
        oldest = self.oldest_untaken(snapshot)
        return {name: at for name in readers if (at := oldest.get(name, math.inf)) < cutoff}

    def oldest_untaken(self, snapshot: RegistrySnapshot) -> Mapping[str, float]:
        """Every claimed reader's oldest untaken message, read again only when something that
        decides it changed: a bus message, a take or claim in coordination, a channel edit that
        moves a delivery scope (the catalog), or a registry cut. One observer pass captures
        activity through several paths; they share this read."""
        key = (self.bus.bus.revision(), FileRevision.capture(self.coordination),
               FileRevision.capture(self.coordination.with_name(self.coordination.name + "-wal")),
               FileRevision.capture(self.catalog))
        if not self.untaken.answers(key, snapshot):
            self.untaken = UntakenCut(
                key, snapshot, self.bus.oldest_untaken(snapshot.claimed_readers(snapshot.threads), snapshot))
        return self.untaken.oldest


@dataclass(frozen=True)
class UntakenCut:
    """The oldest untaken messages read at one bus, coordination and registry cut."""

    key: tuple[object, ...]
    registry: RegistrySnapshot | UnreadRegistry
    oldest: Mapping[str, float]

    def answers(self, key: tuple[object, ...], snapshot: RegistrySnapshot) -> bool:
        """The same cut: the snapshot by identity, since one registry revision decodes one."""
        return self.registry is snapshot and self.key == key


class Comms(ActivityClient):
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
        self.relationships = ThreadRelationships(self.root, self.registry, self.bus, self.views)

    async def watch(
        self, name: str, *, after: InboxStart = TakenInputStart(),
    ) -> AsyncGenerator[Message]:
        """Wait unlocked; each inbox read owns one client call on a worker, so the caller's loop
        keeps serving, and streaming records nothing."""
        reader = await Coordination.run_worker(partial(self.watched_reader, name))
        maintenance = MaintenanceBarrier(self.registry.store.path)
        bus = self.bus.bus
        sources = (*bus.commit_sources(), FileSource(maintenance.state_path))
        async with aclosing(bus.watch_sources(*sources)) as changes:
            async for _ in changes:
                while True:
                    batch = await Coordination.run_worker(
                        partial(self.watched_batch, name, reader, after))
                    for message in batch.page.messages:
                        yield message
                        after = after.advanced(message.reference)
                    if not batch.remaining:
                        break

    @cached_property
    def bus(self) -> MessageBus:
        return MessageBus(self.root, self.registry, OwnerPublisher)

    def activity_store(self) -> InboxActivity:
        return InboxActivity(self.root, self.registry, self.bus)

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
