"""Delivery, indexed reads and history over owned wire/publication components."""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from functools import partial
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractContextManager, ExitStack, aclosing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.bus_activity_index import ActivityClocks, BusActivityIndex
from agent_comms.bus_route_counts import OpenedRouteCounts
from agent_comms.channel_targets import CommandTarget
from agent_comms.channels import AnyChannel
from agent_comms.coordinator import Coordination
from agent_comms.errors import UnregisteredThreadError
from agent_comms.historical_views import HistoryArchive
from agent_comms.locked_store import RuntimeFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHeldError
from agent_comms.message_page import (
    InboxPage,
    MessagePage,
    MessagePageRequest,
    PageBounds,
)
from agent_comms.message_reference import AfterMessageStart, FirstUnreadStart, InboxStart, MessageReference
from agent_comms.messages import Message
from agent_comms.page_traversal import NewerTraversal
from agent_comms.read_basis import (
    AnonymousViewer,
    CapturedSelection,
    DisplayBasis,
    DisplayViewer,
    ListedDeliveries,
    NamedViewer,
    WatchAcknowledgment,
)
from agent_comms.routing import DeliveryScope
from agent_comms.store_files import SharedLock, StoreLockOwner

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.registration import Registration
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.relationships import PeerRelation
    from agent_comms.threads import Thread

from agent_comms.wire_log import (
    FRESH_SNAPSHOT_OPENING,
    CurrentSnapshotOpening,
    WireLog,
    WireSnapshot,
)

from agent_comms.publisher import Publisher
from agent_comms.wire_watch import WatchSource, WireWatch


class HeldBusWrite:
    """Synchronous publications inherit the bus's maintenance wait, never a retry policy."""

    def write_when_open[Result](self, comms: Comms, operation: Callable[[], Result]) -> Result:
        return asyncio.run(comms.bus.when_open(operation))


class WatchPass[Observed](HeldBusWrite, ABC):
    """Observe without the wire lock; enter maintenance only to apply the result."""

    def watch_pass(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        with opened_wire() as comms:
            observed = self.observe(comms)
            self.write_when_open(comms, partial(self.pass_body, comms, observed))

    @abstractmethod
    def observe(self, comms: Comms) -> Observed: ...

    @abstractmethod
    def pass_body(self, comms: Comms, observed: Observed) -> None: ...


class MessageBus[Writes: Publisher](RuntimeFile, StoreLockOwner):
    filename = "wire"

    @property
    def path(self) -> Path:
        return self.path_in(self.log.path.parent)

    def __init__(
        self,
        bus_path: Path,
        registry: Registration,
        publisher: type[Writes],
        *,
        projection: type[OpenedRouteCounts] = OpenedRouteCounts,
    ):
        from agent_comms.catalog_store import ChannelCatalog
        from agent_comms.read_ledger import ReadLedger

        self.reads = ReadLedger.at(bus_path.parent)
        self.projection = projection
        self.log = WireLog(bus_path)
        self.history = HistoryArchive(bus_path)
        self._registry = registry
        self._channels = ChannelCatalog.at(bus_path.parent)
        self.activity_index = self.log.register_append_index(BusActivityIndex(bus_path))
        self.publisher = publisher(self.log, registry, self._channels, self.opened_activity)

    @contextmanager
    def opened_activity(
        self, opening: CurrentSnapshotOpening = FRESH_SNAPSHOT_OPENING,
    ) -> Iterator[ActivityClocks]:
        """Own a fresh bus cut for callers without an existing snapshot."""
        with self.log.opened_wire_snapshot(opening=opening) as source:
            yield self.activity_index.snapshot(source)

    def require_channel(self, target: str):
        """Resolve one declaration against the bus-owned activity cut."""
        with self.opened_activity() as activity:
            return self._channels.read().require_channel(target, activity)

    def channel_declarations(self):
        with self.opened_activity() as activity:
            return self._channels.read().views(activity)

    @contextmanager
    def snapshot(
        self, viewer: DisplayViewer = AnonymousViewer()
    ) -> Iterator[tuple[CapturedSelection, WireSnapshot]]:
        """Keep a fixed bus, namespace and read snapshot after releasing its locks.

        The bus owns capture, including the committed sequence of this exact
        descriptor. Presentation and delivery borrow that same source instead
        of reconstructing it from independently sampled path revisions.
        """
        with ExitStack() as stack:
            with self.locked(mode=SharedLock()):
                source = stack.enter_context(self.log.opened_wire_snapshot())
                with CapturedSelection.reading(
                    self._registry, self._channels, self.reads, viewer,
                    self.activity_index.snapshot(source),
                ) as basis:
                    pass
            yield basis, source

    def inbox(
        self, name: str, target: CommandTarget = AnyChannel.target(), *,
        after: InboxStart = FirstUnreadStart(), bounds: PageBounds = PageBounds(),
    ) -> Sequence[Message]:
        """Read one oldest-first unread page against a newly captured registry."""
        return list(self.inbox_at(
            name, self._registry.snapshot(), target, after=after, bounds=bounds,
        ).page.messages)

    def inbox_at(
        self, name: str, snapshot: RegistrySnapshot, target: CommandTarget = AnyChannel.target(), *,
        after: InboxStart = FirstUnreadStart(), bounds: PageBounds = PageBounds(),
    ) -> InboxPage:
        """Read unread deliveries using the caller's captured registry.

        The route index lists which deliveries are unread, as it does for the
        pending counts, and the page index reads only those rows.
        """
        catalog = self._channels.read()
        delivery = DeliveryScope.capture(name, snapshot, catalog)
        selection = delivery.selection(target, snapshot, self.require_channel)
        document = self.reads.read()
        seen = self.reads.cursor(delivery.actor, snapshot, document=document)
        with self.log.opened_wire_snapshot() as source:
            source.messages_for_references(after.references())
            with self.projection(
                self.log.path, source, snapshot, {delivery.actor: delivery}
            ) as index:
                cursor = after.cursor()
                unread = index.unseen_deliveries(
                    delivery.actor, seen, after=cursor, scope=selection, limit=bounds.limit + 1,
                )
                counts = index.unseen_counts({delivery.actor: seen}, after=cursor)[delivery.actor]
                total = target.pending_count(counts, snapshot, self.require_channel)
                listed = ListedDeliveries(
                    selection,
                    frozenset(row.seq for row in unread),
                    frozenset(row.target for row in unread),
                )
                result = MessagePageRequest(
                    listed, PageBounds(NewerTraversal(cursor), bounds.limit, bounds.max_bytes),
                ).displayed(self.reads, document, delivery.actor, snapshot, snapshot, source)
                return InboxPage(result.page, result.displayed, total - len(result.page.messages))

    async def watch(
        self, name: str, *, acknowledgment: type[WatchAcknowledgment],
        after: InboxStart = FirstUnreadStart(),
    ) -> AsyncIterator[Message]:
        """Yield unread messages, recording only messages the caller takes.

        Notifications and inbox reads use the same owners as native delivery.
        Closing before the next read leaves the last message unread.
        """
        reader = self._registry.require(name).incarnation
        maintenance = MaintenanceBarrier(self._registry.store.path)
        async with aclosing(
            WireWatch.observations(WatchSource.wire(self.log.path.parent),
                                   WatchSource.file(maintenance.state_path))
        ) as changes:
            async for _ in changes:
                maintenance.read().require_open()
                snapshot = self._registry.snapshot()
                while True:
                    batch = await asyncio.to_thread(
                        self.inbox_at, name, snapshot, after=after)
                    if not reader.current(self._registry.snapshot()):
                        raise UnregisteredThreadError("Watched thread was removed or replaced")
                    for message in batch.page.messages:
                        yield message
                        acknowledgment.taken(self.reads, batch.displayed.through(message.seq))
                        after = AfterMessageStart(message.reference)
                    if not batch.remaining:
                        break

    async def when_open[Result](self, operation: Callable[[], Result | Awaitable[Result]]) -> Result:
        """Wait out a hold without repeating a synchronous write.

        Synchronous callbacks run under ingress in a worker, so refusal precedes
        even in-memory mutation and maintenance cannot split a publication batch.
        An async callback only creates its coroutine under ingress; its stream
        runs unlocked and retains its own delivery cursor across held attempts.
        No other failure is retried.
        """
        maintenance = MaintenanceBarrier(self._registry.store.path)

        def execute():
            with self.locked():
                maintenance.assert_open_unlocked()
                return operation()

        while True:
            try:
                result = await Coordination.run_worker(execute)
                return await result if inspect.isawaitable(result) else result
            except MaintenanceHeldError:
                await self.resumed()

    async def resumed(self) -> None:
        """Return once maintenance has cleared; each change to its state wakes the check."""
        maintenance = MaintenanceBarrier(self._registry.store.path)
        watched = WatchSource.file(maintenance.state_path)
        async with aclosing(WireWatch.observations(watched)) as changes:
            async for _ in changes:
                if not maintenance.read().turn_pending:
                    return

    def pending_count(self, name: str, target: CommandTarget = AnyChannel.target()) -> int:
        """Count unread messages without retaining their bodies."""
        with self.snapshot() as (basis, source):
            counts = self._pending_projection(source, basis, [name])[name]
            return target.pending_count(
                counts, basis.registry,
                lambda name: basis.catalog.require_channel(name, basis.activity),
            )

    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        if not names:
            return {}
        with self.snapshot() as (basis, source):
            return {
                name: sum(counts.values())
                for name, counts in self._pending_projection(source, basis, names).items()
            }

    def _pending_projection(
        self, source: WireSnapshot, basis: CapturedSelection, names: Sequence[str],
    ) -> Mapping[str, Mapping[str, int]]:
        """Count deliveries using the same bus, registrations and read positions."""
        actors = {name: basis.registry.canonical_name(name) for name in names}
        deliveries = {
            actor: DeliveryScope.capture(actor, basis.registry, basis.catalog)
            for actor in set(actors.values())
        }
        seen = {
            actor: basis.document.cursor(actor, basis.registry, source) for actor in deliveries
        }
        with self.projection(self.log.path, source, basis.registry, deliveries) as index:
            counts = index.unseen_counts(seen)
        return {name: counts[actor] for name, actor in actors.items()}

    def pending_counts_opened(self, source: WireSnapshot,
                              selection: CapturedSelection, *,
                              conversations: Iterable[str] = ()) -> Mapping[str, int]:
        """Count unread messages using the caller's bus, registrations and read positions."""
        actor = selection.require_viewer()
        seen = selection.seen(source)
        delivery = DeliveryScope.capture(actor, selection.registry, selection.catalog)
        # A sidebar asks for every declared conversation, including exact zeros.
        # Other readers keep the sparse public count contract by passing no names.
        counts = dict.fromkeys(conversations, 0)
        with self.projection(
            self.log.path, source, selection.registry, {actor: delivery}
        ) as index:
            counts.update(index.unseen_counts({actor: seen})[actor])
        return counts

    def delivered(
        self, name: str, references: Sequence[MessageReference]
    ) -> DisplayBasis:
        """The named messages this reader received and has not yet read.

        The canonical sparse public-message reader supplies only named rows,
        including control messages without a wake cohort. No unread listing is
        materialized. The caller marks the returned basis read.
        """
        snapshot = self._registry.snapshot()
        delivery = DeliveryScope.capture(name, snapshot, self._channels.read())
        document = self.reads.read()
        seen = self.reads.cursor(delivery.actor, snapshot, document=document)
        with self.log.opened_wire_snapshot() as source:
            messages = source.messages_for_references(references)
            return source.query_page_index(lambda index: self.reads.capture_indexed(
                name, tuple(message for message in messages
                            if delivery.includes(message) and not delivery.has_read(seen, message, snapshot)),
                snapshot, source, index, document=document))

    def mark_delivered(
        self, name: str, references: Sequence[MessageReference]
    ) -> int:
        """Mark only the named unread messages delivered and return their count."""
        basis = self.delivered(name, references)
        self.reads.mark_displayed(basis.viewer, basis)
        return basis.message_count

    def mark_delivered_through(
        self, name: str, sequence: int, *, target: CommandTarget = AnyChannel.target(),
    ) -> None:
        """Mark the selected inbox through one captured cut, without loading its messages."""
        if sequence < 0:
            raise ValueError("Delivery sequence cannot be negative.")
        with self.snapshot(NamedViewer(name)) as (basis, source):
            canonical = basis.require_viewer()
            delivery = DeliveryScope.capture(canonical, basis.registry, basis.catalog)
            scope = delivery.selection(target, basis.registry,
                lambda channel: basis.catalog.require_channel(channel, basis.activity))
            displayed = source.query_page_index(lambda index: scope.capture_deliveries(
                self.reads, canonical, basis.registry, source, index,
                document=basis.document, audience=delivery, through=sequence,
            ))
            self.reads.mark_displayed(canonical, displayed)

    def awareness_segments(
        self, owner: Thread, relations: Sequence[PeerRelation], *, blocking: bool = False
    ):
        """Bounded pointers to addressed sources, independent of UI read state.

        Pointers from peers whose relevance grants ``Prioritized`` come first;
        which messages are in the window does not depend on relevance.

        Current canonical checkpoint rows own the pointers. Repeated reminders
        are intentional: neither displaying nor composing them proves model read.
        Input preparation may omit this optional supplement on contention;
        inspection waits in its existing resource worker for the actual read.
        """
        from agent_comms.context_segments.awareness import AwarenessSegment, UnavailableAwarenessSegment
        from agent_comms.errors import RelationViolationError
        from agent_comms.peer_relevance import Prioritized
        from agent_comms.private_bus_checkpoint import addressed_source_pointers_unlocked

        try:
            with self.log.checked_read(blocking=blocking) as source:
                marker = source.marker
                rows = addressed_source_pointers_unlocked(
                    self.log, marker, owner.incarnation.lookup
                )
        except (BlockingIOError, RelationViolationError):
            # Contention or a checkpoint/source change during the read; any
            # other failure is a defect in the authoritative bus and raises.
            return (UnavailableAwarenessSegment.capture(),)
        if not rows:
            return ()
        first = {
            relation.peer.lookup
            for relation in relations
            if relation.relevance.grants(Prioritized)
        }
        rows = tuple(sorted(rows, key=lambda row: row.sender_lookup not in first))
        return (AwarenessSegment.capture(self.log.path, marker.root_id, rows),)

    def incoming_page(self, name: str, *, after: int) -> MessagePage:
        """A bounded delivery stream independent of UI read acknowledgments."""
        snapshot = self._registry.snapshot()
        delivery = DeliveryScope.capture(name, snapshot, self._channels.read())
        return self.log.read_page(MessagePageRequest(delivery, PageBounds(NewerTraversal(after))))

    def channels(self) -> Sequence[str]:
        return list(self.channel_declarations())
