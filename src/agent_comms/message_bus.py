"""Delivery and history over the canonical sequence-keyed bus."""

from __future__ import annotations

from agent_comms.activity_clocks import ActivityClocks
import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractContextManager, aclosing, contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.bus import Bus, BusSnapshot
from agent_comms.channel_targets import NamedTarget
from agent_comms.channels import AnyChannel
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordinator import Coordination
from agent_comms.historical_views import HistoryArchive
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHeldError
from agent_comms.message_page import InboxPage, MessagePage, MessagePageRequest, PageBounds
from agent_comms.message_reference import (
    FirstUnreadStart,
    NewestUnreadStart,
    InboxStart,
    MessageReference,
)
from agent_comms.messages import Message
from agent_comms.page_traversal import LiveCursor
from agent_comms.publisher import Publisher
from agent_comms.read_basis import (
    AnonymousViewer,
    CapturedSelection,
    DisplayBasis,
    DisplayViewer,
    ListedDeliveries,
    NamedViewer,
)
from agent_comms.routing import DeliveryScope
from agent_comms.store_files import UNBOUNDED_CONTENTION, StoreLockContention

if TYPE_CHECKING:
    from agent_comms.assignment_states import AssignmentState
    from agent_comms.comms import Comms
    from agent_comms.registration import Registration
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.relationships import PeerRelation
    from agent_comms.threads import Thread


class HeldBusWrite:
    """Maintenance delays an unattempted publication, never retries a write."""

    def write_when_open[Result](self, comms: Comms, operation: Callable[[], Result]) -> Result:
        return asyncio.run(comms.bus.when_open(operation))


class WatchPass[Observed](HeldBusWrite, ABC):
    """Observe before entering maintenance to apply the result."""


    def watch_pass(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        with opened_wire() as comms:
            observed = self.observe(comms)
            self.write_when_open(comms, partial(self.pass_body, comms, observed))

    @abstractmethod
    def observe(self, comms: Comms) -> Observed: ...

    @abstractmethod
    def pass_body(self, comms: Comms, observed: Observed) -> None: ...


class MessageBus[Writes: Publisher]:
    def __init__(
        self, root: Path, registry: Registration, publisher: type[Writes],
    ):
        from agent_comms.catalog_store import ChannelCatalog
        from agent_comms.read_ledger import ReadLedger

        self.bus = Bus(Bus.path_in(root))
        self.reads = ReadLedger.at(root)
        self.history = HistoryArchive(self.bus.path)
        self._registry = registry
        self._channels = ChannelCatalog.at(root)
        self.publisher = publisher(self.bus, registry, self._channels, self.opened_activity)

    @contextmanager
    def opened_activity(
        self, contention: StoreLockContention = UNBOUNDED_CONTENTION,
    ) -> Iterator[ActivityClocks]:
        yield self.bus.activity_clocks(contention)

    def require_channel(self, target: str):
        with self.opened_activity() as activity:
            return self._channels.read().require_channel(target, activity)

    def channel_declarations(self):
        with self.opened_activity() as activity:
            return self._channels.read().views(activity)

    @contextmanager
    def snapshot(self, viewer: DisplayViewer = AnonymousViewer()) -> Iterator[tuple[CapturedSelection, BusSnapshot]]:
        with self.bus.reading() as source, CapturedSelection.reading(
            self._registry, self._channels, self.reads, viewer, source.activity_clocks(),
        ) as basis:
            yield basis, source

    def inbox(
        self, name: str, target: NamedTarget = AnyChannel.target(), *,
        after: InboxStart = NewestUnreadStart(), bounds: PageBounds = PageBounds(),
    ) -> InboxPage:
        """A glance at a thread's unread messages: newest first, with the unread beyond them."""
        return self.inbox_at(name, self._registry.snapshot(), target, after=after, bounds=bounds)

    def inbox_at(
        self, name: str, snapshot: RegistrySnapshot, target: NamedTarget = AnyChannel.target(), *,
        after: InboxStart = FirstUnreadStart(), bounds: PageBounds = PageBounds(),
    ) -> InboxPage:
        catalog = self._channels.read()
        delivery = DeliveryScope.capture(name, snapshot, catalog)
        selection = delivery.selection(target, snapshot, self.require_channel)
        document, seen = after.read_basis(self.reads, delivery.actor, snapshot)
        self.bus.messages_for_references(after.references())
        cursor = after.cursor()
        traversal = after.traversal()
        with self.bus.reading() as source:
            unread = source.unseen_deliveries(
                snapshot, delivery, seen, after=cursor, scope=selection, limit=bounds.limit + 1,
                traversal=traversal,
            )
            counts = source.unseen_counts(
                snapshot, {delivery.actor: delivery}, {delivery.actor: seen}, after=cursor,
            )[delivery.actor]
        total = target.pending_count(counts, snapshot, self.require_channel)
        listed = ListedDeliveries(selection, frozenset(row.seq for row in unread),
                                  frozenset(row.target for row in unread))
        result = MessagePageRequest(
            listed, PageBounds(traversal, bounds.limit),
        ).displayed(self.reads, document, delivery.actor, snapshot, snapshot, self.bus)
        return InboxPage(result.page, result.displayed, total - len(result.page.messages))

    async def when_open[Result](self, operation: Callable[[], Result | Awaitable[Result]]) -> Result:
        maintenance = MaintenanceBarrier(self._registry.store.path)

        def execute():
            maintenance.assert_open_unlocked()
            return operation()

        while True:
            try:
                result = await Coordination.run_worker(execute)
                return await result if inspect.isawaitable(result) else result
            except MaintenanceHeldError:
                await self.resumed()

    async def resumed(self) -> None:
        from .watched import FileSource

        maintenance = MaintenanceBarrier(self._registry.store.path)
        async with aclosing(Bus.watch_sources(FileSource(maintenance.state_path))) as changes:
            async for _ in changes:
                if not maintenance.read().turn_pending:
                    return

    def pending_count(self, name: str, target: NamedTarget = AnyChannel.target()) -> int:
        with self.snapshot() as (basis, source):
            counts = self._pending_projection(source, basis, [name])[name]
            return target.pending_count(counts, basis.registry,
                lambda name: basis.catalog.require_channel(name, basis.activity))

    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        if not names:
            return {}
        with self.snapshot() as (basis, source):
            return {name: sum(counts.values())
                    for name, counts in self._pending_projection(source, basis, names).items()}

    def _pending_projection(self, source: BusSnapshot, basis: CapturedSelection, names: Sequence[str]):
        actors = {name: basis.registry.canonical_name(name) for name in names}
        deliveries = {actor: DeliveryScope.capture(actor, basis.registry, basis.catalog)
                      for actor in set(actors.values())}
        seen = {actor: basis.document.cursor(actor, basis.registry, source)
                for actor in deliveries}
        counts = source.unseen_counts(basis.registry, deliveries, seen)
        return {name: counts[actor] for name, actor in actors.items()}

    def pending_counts_opened(
        self, source: BusSnapshot, selection: CapturedSelection, *, conversations: Iterable[str] = (),
    ) -> Mapping[str, int]:
        actor = selection.require_viewer()
        delivery = DeliveryScope.capture(actor, selection.registry, selection.catalog)
        counts = dict.fromkeys(conversations, 0)
        counts.update(source.unseen_counts(
            selection.registry, {actor: delivery}, {actor: selection.seen(source)},
        )[actor])
        return counts

    def delivered(self, name: str, references: Sequence[MessageReference]) -> DisplayBasis:
        snapshot = self._registry.snapshot()
        delivery = DeliveryScope.capture(name, snapshot, self._channels.read())
        document = self.reads.read()
        seen = self.reads.cursor(delivery.actor, snapshot, document=document)
        messages = self.bus.messages_for_references(references)
        return self.reads.capture(
            name, tuple(message for message in messages if delivery.includes(message)
                        and not delivery.has_read(seen, message, snapshot)),
            snapshot, self.bus, document=document,
        )

    def hand_to_harness(self, name: str, references: Sequence[MessageReference]) -> None:
        """A harness outside Core takes these messages before it says whether they started."""
        self._settle_claims(name, references, lambda state: state.harness_handoff())

    def mark_delivered(self, name: str, references: Sequence[MessageReference]) -> int:
        self._settle_claims(name, references, lambda state: state.harness_delivery())
        basis = self.delivered(name, references)
        self.reads.mark_displayed(basis.viewer, basis)
        return basis.message_count

    def _settle_claims(
        self, name: str, references: Sequence[MessageReference],
        steps: Callable[[AssignmentState], tuple[type[AssignmentState], ...]],
    ) -> None:
        lookup = self._registry.require(name).incarnation.require_recorded().lookup
        with Coordination(str(CoordinationStore.path_in(self.bus.path.parent))) as store:
            store.assignments.settle_harness_inputs(lookup, references, steps)

    def mark_delivered_through(
        self, name: str, sequence: int, *, target: NamedTarget = AnyChannel.target(),
    ) -> None:
        if sequence < 0:
            raise ValueError("Delivery sequence cannot be negative.")
        with self.snapshot(NamedViewer(name)) as (basis, source):
            canonical = basis.require_viewer()
            delivery = DeliveryScope.capture(canonical, basis.registry, basis.catalog)
            scope = delivery.selection(target, basis.registry,
                lambda channel: basis.catalog.require_channel(channel, basis.activity))
            displayed = scope.capture_deliveries(
                self.reads, canonical, basis.registry, source, document=basis.document,
                audience=delivery, through=sequence,
            )
            self.reads.mark_displayed(canonical, displayed)

    def awareness_segments(self, owner: Thread, relations: Sequence[PeerRelation]):
        """Bounded frozen-recipient pointers, independent of UI read state.

        The window is the newest received rows, shown in sequence order. Peer relevance
        orders the selected rows, never changes their window.
        """
        from agent_comms.context_segments.awareness import AwarenessSegment
        from agent_comms.peer_relevance import Prioritized

        first = {relation.peer.require_recorded().lookup for relation in relations
                 if relation.relevance.grants(Prioritized)}
        rows = tuple(
            delivery for record in self.bus.received(
                owner.incarnation.require_recorded().lookup, limit=100,
            ) for delivery in record.deliveries()
        )
        rows = tuple(sorted(
            rows, key=lambda row: (row.sender_lookup not in first, row.seq)))
        return (AwarenessSegment.capture(self.bus.path, rows),) if rows else ()

    def channels(self) -> Sequence[str]:
        return list(self.channel_declarations())
