"""Composition root: state and behavior live on their explicit owners."""

from __future__ import annotations

from agent_comms.execution.context_assembly import ContextAssembly
from agent_comms.execution.input_ingress import InputIngressContext
from agent_comms.execution.input_lookup import InputLookup
from agent_comms.execution.input_text_context import InputTextContext
from agent_comms.messaging.notification_contracts import NotificationEvidenceSource
from agent_comms.messaging.message_notification import CapturedNotificationEvidence

import math
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractContextManager, aclosing
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path
from typing import ClassVar

from agent_comms.context_store import ContextStore
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
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
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegistrySnapshot, UnreadRegistry
from agent_comms.registry.relationships import ThreadRelationships
from agent_comms.registry.route_client import ActivityClient
from agent_comms.registry.thread_configuration import ThreadConfiguration
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_management import ThreadManagement
from agent_comms.registry.threads import Thread
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier
from agent_comms.storage.private_path import FileRevision
from agent_comms.transcript.transcripts import Transcripts


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


class Comms(
    ActivityClient,
    ContextAssembly,
    InputIngressContext[Thread],
    InputTextContext[InputAttempt],
    NotificationEvidenceSource[CapturedNotificationEvidence],
):
    input_dispositions: ClassVar[Callable[[Path], InputLookup[InputAttempt]]] = InputDispositions

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
