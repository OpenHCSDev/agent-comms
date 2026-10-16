"""Delivery, indexed reads and history over owned wire/publication components."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_activity_index import BusActivityIndex, ChannelActivity
from .bus_route_counts import OpenedRouteCounts
from .channel_targets import BuiltinChannel, is_channel_target
from .errors import UnregisteredThreadError
from .historical_views import HistoryArchive
from .message_page import ConversationMessagePageRequest, MessagePage, MessagePageRequest, UnreadBeforeRequest
from .message_reference import MessageReference
from .messages import Message
from .read_basis import (
    ChannelDisplayScope,
    DMDisplayScope,
    ListedDeliveries,
    MessageDisplayScope,
    WatchAcknowledgment,
)
from .routing import DeliveryScope
from .store_files import _store_lock

if TYPE_CHECKING:
    from .presentation import DisplaySelection
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .relationships import PeerRelation
    from .threads import Thread

from .publisher import Publisher
from .wire_log import OpenedWireSnapshot, WireLog


class MessageBus:
    def __init__(
        self,
        bus_path: Path,
        registry: Registration,
        *,
        private_response_writes: bool = False,
        private_initial_writes: bool = False,
        private_claim_writes: bool = False,
    ):
        from .catalog_store import ChannelCatalog
        from .read_ledger import ReadLedger

        self.reads = ReadLedger(bus_path.parent / ReadLedger.filename)
        self.log = WireLog(bus_path)
        self.history = HistoryArchive(bus_path)
        self._registry = registry
        self._channels = ChannelCatalog(bus_path.parent / ChannelCatalog.filename)
        self._activity = self.log.register_append_index(BusActivityIndex(bus_path))
        self.publisher = Publisher(
            self.log,
            registry,
            self._channels,
            private_response_writes=private_response_writes,
            private_initial_writes=private_initial_writes,
            private_claim_writes=private_claim_writes,
        )

    def _activity_clocks(self, source: OpenedWireSnapshot | None = None):
        """The existing index borrows the same opened source after release."""
        if source is not None:
            return self._activity.snapshot(source, self._bus_activity_fields)
        with self.log._opened_wire_snapshot(
            need_sequence=False, prepared_indexes=self.log.prepare_append_indexes(),
        ) as opened:
            return self._activity_clocks(opened)

    def channel_activity(self) -> Mapping[str, ChannelActivity]:
        """Aggregate clocks from the single existing append-aware source cache."""
        channels, _ = self._activity_clocks()
        return {name: ChannelActivity(*clocks) for name, clocks in channels.items()}

    @staticmethod
    def _bus_activity_fields(record: Mapping[str, object]):
        from .wire_record import WireRecord
        for message in WireRecord.public_from_wire(record).messages():
            yield (
                message.sender,
                message.target,
                message.timestamp,
                not message.sender_role.executable,
                message.membership is None and not message.notice,
            )

    @contextmanager
    def snapshot(
        self, viewer: str | None = None
    ) -> Iterator[tuple[DisplaySelection, OpenedWireSnapshot]]:
        """Own the original bus/namespace/read cut, beyond physical custody.

        The bus owns capture, including the committed sequence of this exact
        descriptor. Presentation and delivery borrow that same source instead
        of reconstructing it from independently sampled path revisions.
        """
        from .presentation import DisplaySelection

        prepared = self.log.prepare_append_indexes()
        with ExitStack() as stack:
            with _store_lock(self.log.path.parent / "wire", shared=True):
                source = stack.enter_context(self.log._opened_wire_snapshot(
                    prepared_indexes=prepared,
                ))
                with DisplaySelection.reading(
                    self._registry, self._channels, self.reads, viewer
                ) as basis:
                    pass
            yield basis, source

    def inbox(
        self, name: str, target: str | None = None, *,
        snapshot: RegistrySnapshot | None = None,
    ) -> Sequence[Message]:
        """Unread messages delivered to a thread, read without scanning the log.

        The route index lists which deliveries are unread, as it does for the
        pending counts, and the page index reads only those rows.
        """
        if snapshot is None:
            snapshot = self._registry.snapshot()
        catalog = self._channels.read()
        delivery = DeliveryScope.capture(name, snapshot, catalog)
        selection = delivery.selection(target, snapshot, catalog)
        seen = self.reads.seen_sequences(delivery.actor, snapshot)
        with self.log._opened_wire_snapshot() as source:
            with OpenedRouteCounts(
                self.log.path, source, snapshot, {delivery.actor: delivery}
            ) as index:
                unread = index.unseen_deliveries(delivery.actor, seen)
            if not unread:
                return []
            listed = ListedDeliveries(
                selection,
                frozenset(row.seq for row in unread),
                frozenset(row.target for row in unread),
            )
            page = MessagePageRequest.every(listed, after=min(listed.sequences) - 1).read_opened(source)
            return list(page.messages)

    async def watch(self, name: str, *, acknowledgment: type[WatchAcknowledgment]) -> AsyncIterator[Message]:
        """Yield unread messages, recording only messages the caller takes.

        Notifications and inbox reads use the same owners as native delivery.
        Closing before the next read leaves the last message unread.
        """
        from contextlib import aclosing
        from .wire_watch import WatchSource, WireWatch

        reader = self._registry.require(name).incarnation
        async with aclosing(
            WireWatch.observations(WatchSource.wire(self.log.path.parent))
        ) as changes:
            async for _ in changes:
                snapshot = self._registry.snapshot()
                if not reader.current(snapshot):
                    raise UnregisteredThreadError("Watched thread was removed or replaced")
                for message in self.inbox(name, snapshot=snapshot):
                    displayed = self.reads.capture(name, (message,), snapshot, self.log.path)
                    yield message
                    acknowledgment.taken(self.reads, displayed)

    def pending_count(self, name: str, target: str | None = None) -> int:
        """Count unread messages without retaining their bodies."""
        with self.snapshot() as (basis, source):
            counts = self._pending_projection(source, basis, [name])[name]
            if target is None or BuiltinChannel.aggregate_target(target):
                return sum(counts.values())
            if is_channel_target(target):
                targets = basis.catalog.history_targets(target)
                return sum(
                    count for scope, count in counts.items() if targets is None or scope in targets
                )
            return counts.get(basis.registry.require(target).name, 0)

    def pending_counts(self, name: str) -> Mapping[str, int]:
        with self.snapshot() as (basis, source):
            return self._pending_projection(source, basis, [name])[name]

    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        if not names:
            return {}
        with self.snapshot() as (basis, source):
            return {
                name: sum(counts.values())
                for name, counts in self._pending_projection(source, basis, names).items()
            }

    def _pending_projection(
        self, source: OpenedWireSnapshot, basis: DisplaySelection, names: Sequence[str],
    ) -> Mapping[str, Mapping[str, int]]:
        """Bulk delivery counts from one original source, namespace and ledger."""
        actors = {name: basis.registry.canonical_name(name) for name in names}
        deliveries = {
            actor: DeliveryScope.capture(actor, basis.registry, basis.catalog)
            for actor in set(actors.values())
        }
        seen = {
            actor: basis.document.seen_sequences(
                actor, basis.registry, source.revision.identity if source.revision else None
            ) for actor in deliveries
        }
        with OpenedRouteCounts(self.log.path, source, basis.registry, deliveries) as index:
            counts = index.unseen_counts(seen)
        return {name: counts[actor] for name, actor in actors.items()}

    def pending_counts_opened(self, source: OpenedWireSnapshot,
                              selection: DisplaySelection) -> Mapping[str, int]:
        """Borrow the caller's original source, namespace and sparse reads."""
        actor = selection.require_viewer()
        delivery = DeliveryScope.capture(actor, selection.registry, selection.catalog)
        with OpenedRouteCounts(
            self.log.path, source, selection.registry, {actor: delivery}
        ) as index:
            return index.unseen_counts({actor: selection.seen})[actor]

    def mark_delivered(
        self, name: str, references: Sequence[MessageReference]
    ) -> int:
        """Mark only the named unread messages delivered and return their count."""
        requested = frozenset(references)
        snapshot = self._registry.snapshot()
        messages = tuple(
            message
            for message in self.inbox(name, snapshot=snapshot)
            if message.reference in requested
        )
        basis = self.reads.capture(name, messages, snapshot, self.log.path)
        self.reads.mark_displayed(basis.viewer, basis)
        return len(messages)

    def mark_delivered_through(self, name: str, sequence: int) -> None:
        """Advance a thread's global inbox cursor without loading messages."""
        snapshot = self._registry.snapshot()
        canonical = snapshot.require(name).name
        if sequence < 0:
            raise ValueError("Delivery sequence cannot be negative.")
        messages = (message for message in self.inbox(canonical, snapshot=snapshot)
                    if message.seq <= sequence)
        displayed = self.reads.capture(canonical, messages, snapshot, self.log.path)
        self.reads.mark_displayed(canonical, displayed)
        return

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, either possibly deleted, in seq order."""
        snapshot = self._registry.snapshot()
        scope = DMDisplayScope.capture(
            snapshot.participant(a).name, snapshot.participant(b).name, snapshot
        )
        return [message for message in self.log.full_history() if scope.includes(message)]

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full target-owned channel history, independent of delivery membership."""
        if not is_channel_target(target):
            raise ValueError(f"{target!r} is not a channel target.")
        scope = ChannelDisplayScope(target, self._channels.read().history_targets(target))
        return [message for message in self.log.full_history() if scope.includes(message)]

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
            relation.peer.incarnation.lookup
            for relation in relations
            if relation.relevance.grants(Prioritized)
        }
        rows = tuple(sorted(rows, key=lambda row: row.sender_lookup not in first))
        return (AwarenessSegment.capture(self.log.path, marker.root_id, rows),)

    def incoming_page(self, name: str, *, after: int, limit: int = 100) -> MessagePage:
        """A bounded delivery stream independent of UI read acknowledgments."""
        snapshot = self._registry.snapshot()
        delivery = DeliveryScope.capture(name, snapshot, self._channels.read())
        return self.display_page(delivery, after=after, limit=limit)

    def dm_history_page(
        self,
        a: str,
        b: str,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Return one bounded page between two threads, either possibly deleted."""
        snapshot = self._registry.snapshot()
        scope = DMDisplayScope.capture(
            snapshot.participant(a).name, snapshot.participant(b).name, snapshot
        )
        return self.display_page(
            scope, before=before, after=after, limit=limit, max_bytes=max_bytes
        )

    def channel_history_page(
        self,
        target: str,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Return one bounded page from a channel in ascending order."""
        if not (is_channel_target(target)):
            raise ValueError(f"{target!r} is not a channel target.")
        targets = self._channels.read().history_targets(target)
        return self.display_page(
            ChannelDisplayScope(target, targets),
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def display_page(
        self,
        scope: MessageDisplayScope,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Browse a captured presentation scope, not a delivery/history authority."""
        return MessagePageRequest.capture(
            scope, before=before, after=after, limit=limit, max_bytes=max_bytes
        ).read(self.log)

    def conversation_page(
        self,
        scope: MessageDisplayScope,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Present only records whose declaring kind is conversation content."""
        return ConversationMessagePageRequest.capture(
            scope, before=before, after=after, limit=limit, max_bytes=max_bytes
        ).read(self.log)

    def conversation_older_unread(
        self, scope: DMDisplayScope, *, before: int, seen: frozenset[int]
    ) -> bool:
        """Ask the scoped offset reader, newest first, stopping at the first unread."""
        return UnreadBeforeRequest(scope, before, seen).read(self.log)

    def last_sent_timestamps(self, *, source: OpenedWireSnapshot | None = None) -> Mapping[str, float]:
        """Reuse the same verified activity source; no second projection or body cache."""
        _, sent = self._activity_clocks(source)
        return dict(sent)

    def full_history_page(
        self,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Bounded combined view of explicit channel and direct wire messages."""
        return self.display_page(
            ChannelDisplayScope(BuiltinChannel.ANY.value, BuiltinChannel.ANY.history_targets),
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def channels(self) -> Sequence[str]:
        return list(self._channels.read().views(self._registry.all_threads()))
