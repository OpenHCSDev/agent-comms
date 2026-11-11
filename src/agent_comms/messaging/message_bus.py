from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import aclosing, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from agent_comms.context_segments.awareness import AwarenessSegment
from agent_comms.coordination.coordinator import Coordination
from agent_comms.messaging.agent_inbox import TakenInputCursor
from agent_comms.messaging.bus import Bus, BusSnapshot
from agent_comms.messaging.bus_read_source import BusReadSource
from agent_comms.messaging.bus_route_counts import HeldUnseenCounts, HeldUnseenOldest
from agent_comms.messaging.channel_targets import NamedTarget
from agent_comms.messaging.channels import AnyChannel, Channel
from agent_comms.messaging.historical_views import HistoryArchive
from agent_comms.messaging.message_page import (
    DisplayedMessagePage,
    InboxPage,
    MessagePageRequest,
    PageBounds,
)
from agent_comms.messaging.message_reference import FirstUnreadStart, InboxStart, NewestUnreadStart
from agent_comms.messaging.published_bus import PublishedBus
from agent_comms.messaging.publisher import Publisher
from agent_comms.messaging.read_basis import (
    AnonymousViewer,
    DisplayViewer,
    ListedDeliveries,
    NamedViewer,
    ReadCursor,
)
from agent_comms.messaging.read_ledger import ReadDocument, ReadLedger
from agent_comms.messaging.read_selection import CapturedSelection
from agent_comms.messaging.routing import DeliveryScope
from agent_comms.messaging.watched import FileSource
from agent_comms.peer_relevance import Prioritized
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.peer_relation import PeerRelation
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceHeldError


@dataclass(frozen=True)
class UnreadCut:
    """One unread page's rows in one source cut; a count of the rest must read the same cut."""

    source: BusSnapshot
    snapshot: RegistrySnapshot
    require_channel: Callable[[str], Channel]
    delivery: DeliveryScope
    seen: ReadCursor
    cursor: int
    request: MessagePageRequest
    document: ReadDocument

    def counts(self) -> dict[str, int]:
        """Inside the cut: what is pending for this thread, per conversation."""
        actor = self.delivery.actor
        counts: dict[str, int] = self.source.unseen_counts(
            self.snapshot, {actor: self.delivery}, {actor: self.seen}, after=self.cursor,
        )[actor]
        return counts


class MessageBus[Writes: Publisher](PublishedBus[Writes], BusReadSource):
    def __init__(
        self, root: Path, registry: Registration, publisher: type[Writes],
    ):
        self.bus = Bus(Bus.path_in(root))
        self.reads = ReadLedger.at(root)
        self.history = HistoryArchive.at(self.bus.path.parent)
        self._registry = registry
        self._channels = ChannelCatalog.at(root)
        self._pending = HeldUnseenCounts()
        self.publisher = publisher(self.bus, registry, self._channels, self.opened_activity)

    @contextmanager
    def snapshot(self, viewer: DisplayViewer = AnonymousViewer()) -> Iterator[tuple[CapturedSelection, BusSnapshot]]:
        with self.bus.reading() as source:
            basis = CapturedSelection.load(
                self._registry, self._channels, self.reads, viewer, source.activity_clocks(),
            )
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
        with self._unread_cut(name, snapshot, target, after, bounds) as cut:
            counts = cut.counts()
        result = self._displayed(cut)
        total = target.pending_count(counts, snapshot, cut.require_channel)
        return InboxPage(result.page, result.displayed, total - len(result.page.messages))

    def untaken_at(
        self, name: str, snapshot: RegistrySnapshot, target: NamedTarget = AnyChannel.target(), *,
        after: InboxStart = FirstUnreadStart(), bounds: PageBounds = PageBounds(),
    ) -> DisplayedMessagePage:
        """The page alone. A watch wakes on every bus commit and never reads the remainder:
        counting it was a second full route query per empty wake per open socket."""
        with self._unread_cut(name, snapshot, target, after, bounds) as cut:
            pass
        return self._displayed(cut)

    def _displayed(self, cut: UnreadCut) -> DisplayedMessagePage:
        page: DisplayedMessagePage = cut.request.displayed(
            self.reads, cut.document, cut.delivery.actor, cut.snapshot, cut.snapshot, self.bus,
        )
        return page

    @contextmanager
    def _unread_cut(
        self, name: str, snapshot: RegistrySnapshot, target: NamedTarget, after: InboxStart,
        bounds: PageBounds,
    ) -> Iterator[UnreadCut]:
        catalog = self._channels.read()
        require_channel = partial(self.channel_in, catalog)
        delivery = DeliveryScope.capture(name, snapshot, catalog)
        selection = delivery.selection(target, snapshot, require_channel)
        document, seen = after.read_basis(self.reads, delivery.actor, snapshot)
        self.bus.messages_for_references(after.references())
        cursor = after.cursor()
        traversal = after.traversal()
        with self.bus.reading() as source:
            with source.observing() as db:
                seen = seen.attached(db, self.bus.path.parent, self._registry.store.privacy())
            unread = source.unseen_deliveries(
                snapshot, delivery, seen, after=cursor, scope=selection, limit=bounds.limit + 1,
                traversal=traversal,
            )
            yield UnreadCut(
                source, snapshot, require_channel, delivery, seen, cursor,
                MessagePageRequest.listed(
                    ListedDeliveries(selection, frozenset(row.seq for row in unread),
                                     frozenset(row.target for row in unread)),
                    PageBounds(traversal, bounds.limit),
                ), document,
            )

    def advanced_oldest_untaken(
        self, held: HeldUnseenOldest, names: Sequence[str], snapshot: RegistrySnapshot,
    ) -> HeldUnseenOldest:
        """Advance every reader's oldest untaken message under its actual inbox cursor."""
        if not names:
            return HeldUnseenOldest()
        catalog = self._channels.read()
        deliveries = {delivery.actor: delivery
                      for delivery in (DeliveryScope.capture(name, snapshot, catalog) for name in names)}
        coordinator = TakenInputCursor.revision(self.bus.path.parent)
        with self.bus.reading() as source:
            with source.observing() as db:
                taken = TakenInputCursor().attached(db, self.bus.path.parent, self._registry.store.privacy())
            return source.advanced_unseen_oldest(
                held, snapshot, deliveries, dict.fromkeys(deliveries, taken),
                source=source.identity, coordinator=coordinator,
            )

    async def when_open[Result](self, operation: Callable[[], Result | Awaitable[Result]]) -> Result:
        while True:
            try:
                result = await Coordination.run_worker(operation)
                return await result if inspect.isawaitable(result) else result
            except MaintenanceHeldError:
                await self.resumed()

    async def resumed(self) -> None:
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
        # Every viewer snapshot asks; between two of them only appended rows can be new.
        self._pending = source.advanced_unseen_counts(
            self._pending, selection.registry, {actor: delivery}, {actor: selection.seen(source)})
        return {**dict.fromkeys(conversations, 0), **self._pending.counts[actor]}

    def mark_read_through(
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
