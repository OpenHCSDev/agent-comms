"""Delivery, indexed reads and history over owned wire/publication components."""

from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import ExitStack, aclosing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_activity_index import ActivityClocks, BusActivityIndex
from .bus_route_counts import OpenedRouteCounts
from .channel_targets import ANY_CHANNEL, BuiltinChannel, ChannelTarget, CommandTarget
from .errors import UnregisteredThreadError
from .maintenance_barrier import MaintenanceBarrier
from .historical_views import HistoryArchive
from .message_page import (
    NEWEST_PAGE, DisplayedMessagePage, MessagePage, MessagePageRequest, PageBounds,
)
from .page_traversal import NewerTraversal
from .message_reference import MessageReference
from .messages import Message
from .read_basis import (
    AnonymousViewer,
    DisplayViewer,
    AllChannelDisplayScope, ChannelDisplayScope,
    DMDisplayScope,
    ListedDeliveries,
    DisplayBasis,
    WatchAcknowledgment,
    CapturedSelection,
)
from .routing import DeliveryScope
from .store_files import SharedLock, _store_lock

if TYPE_CHECKING:
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .relationships import PeerRelation
    from .threads import Thread

from .publisher import Publisher
from .wire_log import CurrentSnapshotOpening, WireSnapshot, WireLog
from .wire_watch import WatchSource, WireWatch


class MessageBus[Writes: Publisher]:
    lock_rank = 0

    @property
    def path(self) -> Path:
        return self.log.path.parent / "wire"

    def __init__(
        self,
        bus_path: Path,
        registry: Registration,
        publisher: type[Writes],
        *,
        projection: type[OpenedRouteCounts] = OpenedRouteCounts,
    ):
        from .catalog_store import ChannelCatalog
        from .read_ledger import ReadLedger

        self.reads = ReadLedger.at(bus_path.parent)
        self.projection = projection
        self.log = WireLog(bus_path)
        self.history = HistoryArchive(bus_path)
        self._registry = registry
        self._channels = ChannelCatalog.at(bus_path.parent)
        self.activity_index = self.log.register_append_index(BusActivityIndex(bus_path))
        self.publisher = publisher(self.log, registry, self._channels)

    @contextmanager
    def opened_activity(self) -> Iterator[ActivityClocks]:
        """Own a fresh bus cut for callers without an existing snapshot."""
        with self.log.opened_wire_snapshot(
            opening=CurrentSnapshotOpening(), prepared_indexes=self.log.prepare_append_indexes(),
        ) as source:
            yield self.activity_index.snapshot(source)

    @contextmanager
    def snapshot(
        self, viewer: DisplayViewer = AnonymousViewer()
    ) -> Iterator[tuple[CapturedSelection, WireSnapshot]]:
        """Keep a fixed bus, namespace and read snapshot after releasing its locks.

        The bus owns capture, including the committed sequence of this exact
        descriptor. Presentation and delivery borrow that same source instead
        of reconstructing it from independently sampled path revisions.
        """
        prepared = self.log.prepare_append_indexes()
        with ExitStack() as stack:
            with _store_lock(self.log.path.parent / "wire", mode=SharedLock()):
                source = stack.enter_context(self.log.opened_wire_snapshot(
                    prepared_indexes=prepared,
                ))
                with CapturedSelection.reading(
                    self._registry, self._channels, self.reads, viewer
                ) as basis:
                    pass
            yield basis, source

    def inbox(
        self, name: str, target: CommandTarget = ANY_CHANNEL, *,
        after: MessageReference | None = None,
    ) -> Sequence[Message]:
        """Read unread deliveries against a newly captured registry."""
        return list(self.inbox_at(name, self._registry.snapshot(), target, after=after).page.messages)

    def inbox_at(
        self, name: str, snapshot: RegistrySnapshot, target: CommandTarget = ANY_CHANNEL, *,
        after: MessageReference | None = None,
    ) -> DisplayedMessagePage:
        """Read unread deliveries using the caller's captured registry.

        The route index lists which deliveries are unread, as it does for the
        pending counts, and the page index reads only those rows.
        """
        catalog = self._channels.read()
        delivery = DeliveryScope.capture(name, snapshot, catalog)
        selection = delivery.selection(target, snapshot, catalog)
        document = self.reads.read()
        seen = self.reads.cursor(delivery.actor, snapshot, document=document)
        with self.log.opened_wire_snapshot() as source:
            source.messages_for_references((after,) if after is not None else ())
            with self.projection(
                self.log.path, source, snapshot, {delivery.actor: delivery}
            ) as index:
                unread = index.unseen_deliveries(delivery.actor, seen, after=after.seq if after else 0)
            listed = ListedDeliveries(
                selection,
                frozenset(row.seq for row in unread),
                frozenset(row.target for row in unread),
            )
            return MessagePageRequest.every(listed, after=min(listed.sequences, default=1) - 1).displayed(
                self.reads, document, delivery.actor, snapshot, snapshot, source,
            )

    async def watch(self, name: str, *, acknowledgment: type[WatchAcknowledgment], after: MessageReference | None = None) -> AsyncIterator[Message]:
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
                batch = await asyncio.to_thread(
                    self.inbox_at, name, snapshot, after=after)
                if not reader.current(self._registry.snapshot()):
                    raise UnregisteredThreadError("Watched thread was removed or replaced")
                for message in batch.page.messages:
                    yield message
                    acknowledgment.taken(self.reads, batch.displayed.through(message.seq))
                    after = message.reference

    async def resumed(self) -> None:
        """Return once maintenance has cleared; each change to its state wakes the check."""
        maintenance = MaintenanceBarrier(self._registry.store.path)
        watched = WatchSource.file(maintenance.state_path)
        async with aclosing(WireWatch.observations(watched)) as changes:
            async for _ in changes:
                if not maintenance.read().turn_pending:
                    return

    def pending_count(self, name: str, target: CommandTarget = ANY_CHANNEL) -> int:
        """Count unread messages without retaining their bodies."""
        with self.snapshot() as (basis, source):
            counts = self._pending_projection(source, basis, [name])[name]
            return target.pending_count(counts, basis.registry, basis.catalog)

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
        """Bulk delivery counts from one original source, namespace and ledger."""
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
                              selection: CapturedSelection) -> Mapping[str, int]:
        """Borrow the caller's original source, namespace and sparse reads."""
        actor = selection.require_viewer()
        delivery = DeliveryScope.capture(actor, selection.registry, selection.catalog)
        with self.projection(
            self.log.path, source, selection.registry, {actor: delivery}
        ) as index:
            return index.unseen_counts({actor: selection.seen(source)})[actor]

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

    def mark_delivered_through(self, name: str, sequence: int) -> None:
        """Mark delivered inbox messages through the requested source sequence."""
        snapshot = self._registry.snapshot()
        canonical = snapshot.require(name).name
        if sequence < 0:
            raise ValueError("Delivery sequence cannot be negative.")
        batch = self.inbox_at(canonical, snapshot)
        self.reads.mark_displayed(canonical, batch.displayed.through(sequence))
        return

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
        from .errors import RelationViolationError
        from .private_bus_checkpoint import addressed_source_pointers_unlocked
        from .context_segments.awareness import AwarenessSegment, UnavailableAwarenessSegment
        from .peer_relevance import Prioritized

        try:
            with self.log.certified_read(blocking=blocking) as source:
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

    def dm_history_page(self, a: str, b: str, bounds: PageBounds = NEWEST_PAGE) -> MessagePage:
        """Return one bounded page between two threads, either possibly deleted."""
        snapshot = self._registry.snapshot()
        scope = DMDisplayScope.capture(
            snapshot.participant(a).name, snapshot.participant(b).name, snapshot
        )
        return self.log.read_page(MessagePageRequest(scope, bounds))

    def channel_history_page(
        self, target: ChannelTarget, bounds: PageBounds = NEWEST_PAGE
    ) -> MessagePage:
        """Return one bounded page from a channel in ascending order."""
        channel = self._channels.read().resolve(target.name)
        scope = channel.history_scope()
        return self.log.read_page(MessagePageRequest(scope, bounds))

    def full_history_page(self, bounds: PageBounds = NEWEST_PAGE) -> MessagePage:
        """Bounded combined view of explicit channel and direct wire messages."""
        scope = AllChannelDisplayScope(BuiltinChannel.ANY.value)
        return self.log.read_page(MessagePageRequest(scope, bounds))

    def channels(self) -> Sequence[str]:
        return list(self._channels.read().views(self._registry.all_threads()))
