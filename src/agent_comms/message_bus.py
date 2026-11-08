"""Delivery and history over the canonical sequence-keyed bus."""

from __future__ import annotations

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
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path

from agent_comms.activity_clocks import ActivityClocks
from agent_comms.agent_inbox import TakenInputCursor
from agent_comms.bus import Bus, BusSnapshot
from agent_comms.bus_route_counts import HeldUnseenCounts
from agent_comms.catalog_document import CatalogAccess
from agent_comms.catalog_store import ChannelCatalog
from agent_comms.channel_targets import ChannelTarget, NamedTarget
from agent_comms.channels import AnyChannel, Channel
from agent_comms.child_process import AsyncWork
from agent_comms.context_segments.awareness import AwarenessSegment
from agent_comms.coordination.coordinator import Coordination
from agent_comms.historical_views import HistoryArchive
from agent_comms.message_page import DisplayedMessagePage, InboxPage, MessagePageRequest, PageBounds
from agent_comms.message_reference import (
    FirstUnreadStart,
    InboxStart,
    NewestUnreadStart,
)
from agent_comms.watch_phase import WatchPhase
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.peer_relevance import Prioritized
from agent_comms.publisher import Publisher
from agent_comms.published_bus import PublishedBus
from agent_comms.read_basis import (
    AnonymousViewer,
    DisplayViewer,
    ListedDeliveries,
    NamedViewer,
    ReadCursor,
)
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.read_selection import CapturedSelection
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.routing import DeliveryScope
from agent_comms.storage.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceHeldError,
)

from .watched import FileSource

from agent_comms.registry.registration import Registration
from agent_comms.registry.threads import Thread
from agent_comms.relationships import PeerRelation
from agent_comms.route_client import RouteClient
from agent_comms.publisher import OwnerPublisher


class BusClient(RouteClient):
    """The selected route owns one cached bus, without the other Core stores."""

    @cached_property
    def bus(self) -> MessageBus:
        return MessageBus(self.root, self.registry, OwnerPublisher)


class Watch[Wire: BusClient](AsyncWork, WatchPhase, DeclaredFamily, ABC):
    """Observe, then apply the result while the bus is open.

    A hold is waited out on the service's own loop, where its stop cancels the wait. Waiting
    in the pass's worker thread kept the connector alive after SIGTERM: the install holding
    maintenance waits for that stop, so systemd killed it at 90 s (installs 17 and 18).
    """

    @abstractmethod
    async def run(self, opened_wire: Callable[[], AbstractContextManager[Wire]]) -> None: ...

    def failed(self, opened_wire: Callable[[], AbstractContextManager[Wire]],
               error: Exception) -> None:
        """A service may report its failed pass; ordinary watches need no extra action."""


class JoinedWatch[Wire: BusClient](Watch[Wire]):
    """The whole pass is one joined operation under maintenance."""

    async def run(self, opened_wire: Callable[[], AbstractContextManager[Wire]]) -> None:
        with opened_wire() as comms:
            await comms.bus.when_open(partial(
                self.enter_phase, comms.root, partial(self.watch_pass, opened_wire)))

    @abstractmethod
    def watch_pass(self, opened_wire: Callable[[], AbstractContextManager[Wire]]) -> None: ...


class PreparedWatchPass[Wire: BusClient, Prepared, Observed](Watch[Wire]):
    """Joined effects finish; a source-proved safe read can drop observation."""

    async def run(self, opened_wire: Callable[[], AbstractContextManager[Wire]]) -> None:
        with opened_wire() as comms:
            prepared = await comms.bus.when_open(partial(
                self.enter_phase, comms.root, partial(self.prepare, comms)))
            observed = await self.run_async(partial(
                self.enter_phase, comms.root, partial(self.observe, comms, prepared)))
            await comms.bus.when_open(partial(
                self.enter_phase, comms.root, partial(self.pass_body, comms, observed)))

    @abstractmethod
    def prepare(self, comms: Wire) -> Prepared: ...

    @abstractmethod
    def observe(self, comms: Wire, prepared: Prepared) -> Observed: ...

    @abstractmethod
    def pass_body(self, comms: Wire, observed: Observed) -> None: ...


class WatchPass[Wire: BusClient, Observed](PreparedWatchPass[Wire, None, Observed]):
    """A phased watch with no acquisition; only watches with real inputs override preparation."""

    def prepare(self, comms: Wire) -> None:
        pass


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


class MessageBus[Writes: Publisher](PublishedBus[Writes]):
    def __init__(
        self, root: Path, registry: Registration, publisher: type[Writes],
    ):
        super().__init__(root, registry, publisher)
        self.history = HistoryArchive.at(self.bus.path.parent)
        self._pending = HeldUnseenCounts()

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

    def oldest_untaken(self, names: Sequence[str], snapshot: RegistrySnapshot) -> Mapping[str, float]:
        """When each thread's oldest message no harness took was sent: one read for them all,
        under the taken-input rule that owns the agent inbox."""
        if not names:
            return {}
        catalog = self._channels.read()
        deliveries = {delivery.actor: delivery
                      for delivery in (DeliveryScope.capture(name, snapshot, catalog) for name in names)}
        with self.bus.reading() as source:
            with source.observing() as db:
                taken = TakenInputCursor().attached(db, self.bus.path.parent, self._registry.store.privacy())
            return source.unseen_oldest(snapshot, deliveries, dict.fromkeys(deliveries, taken))

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
