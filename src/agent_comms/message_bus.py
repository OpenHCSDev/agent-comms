"""Delivery, indexed reads and history over owned wire/publication components."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_activity_index import BusActivityIndex, ChannelActivity
from .bus_route_counts import ActorSeen, BusRouteCounts, PendingRoute
from .channel_targets import BuiltinChannel, is_channel_target
from .errors import UnregisteredThreadError
from .historical_views import HistoryArchive
from .message_page import MessagePage, MessagePageRequest
from .messages import Message
from .read_basis import (
    ChannelDisplayScope,
    DMDisplayScope,
    MessageDisplayScope,
    ViewUnread,
)
from .routing import DeliveryMessage, DeliveryScope, PendingCounts
from .store_files import file_revision

if TYPE_CHECKING:
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .presentation import DisplaySelection
    from .catalog_store import CatalogDocument

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
        self._pending_cache: dict[str, PendingCounts] = {}
        self._view_unread_cache: dict[str, ViewUnread] = {}
        self._activity = BusActivityIndex(bus_path)
        self.publisher = Publisher(
            self.log,
            registry,
            self._channels,
            private_response_writes=private_response_writes,
            private_initial_writes=private_initial_writes,
            private_claim_writes=private_claim_writes,
        )

    def view_unread_counts(
        self,
        viewer: str,
        *,
        display_scopes: tuple[ChannelDisplayScope, ...] | None = None,
        viewer_names: frozenset[str] | None = None,
    ) -> Mapping[str, int]:
        """Human view cursors are independent of executors consuming their inboxes."""
        viewer = self._registry.require(viewer).name
        if display_scopes is None:
            seen = self.reads.seen_sequences(viewer, self._registry.snapshot())
            catalog = self._channels.read()
            scopes = tuple(
                ChannelDisplayScope(
                    channel.name, catalog.history_targets(channel.name), seen_sequences=seen
                )
                for channel in catalog.views(self._registry.all_threads()).values()
            )
        else:
            scopes = display_scopes
        revision = (file_revision(self.log.path), viewer_names)
        cached = self._view_unread_cache.get(viewer)
        if cached is not None and cached.revision == revision and cached.scopes == scopes:
            return dict(cached.counts)
        counts = dict.fromkeys((scope.channel for scope in scopes), 0)
        with self.log._record_snapshot() as (_, records):
            for message, _ in records:
                if message.sender in (viewer_names if viewer_names is not None else {viewer}):
                    continue
                for scope in scopes:
                    if scope.unread(message):
                        counts[scope.channel] += 1
        self._view_unread_cache[viewer] = ViewUnread(revision, scopes, counts)
        return dict(counts)

    def _activity_clocks(self, source: OpenedWireSnapshot | None = None):
        """The existing index borrows the original captured source after release."""
        if source is not None:
            return self._activity.snapshot(source.revision, source.stream, self._bus_activity_fields)
        with self.log._opened_wire_snapshot(need_sequence=False) as opened:
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

    def _delivery_scope(self, name: str, snapshot: RegistrySnapshot | None = None,
                        *, catalog: CatalogDocument | None = None) -> DeliveryScope:
        snapshot = snapshot or self._registry.snapshot()
        canonical = snapshot.canonical_name(name)
        if canonical not in snapshot.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        thread = snapshot.threads[canonical]
        return DeliveryScope(
            thread.name, snapshot.aliases,
            (self._channels.read() if catalog is None else catalog).targets_for(thread.tags)
        )

    def inbox(self, name: str, target: str | None = None) -> Sequence[Message]:
        snapshot = self._registry.snapshot()
        delivery = self._delivery_scope(name, snapshot)
        selection = delivery.selection(target, snapshot, self._channels.read())
        seen = self.reads.seen_sequences(delivery.actor, snapshot)
        with self.log.delivery_snapshot() as originals:
            return [
                item.message
                for item in originals
                if delivery.current(item, snapshot)
                and selection.includes(item.message)
                and item.message.seq not in seen
            ]

    def pending_count(self, name: str, target: str | None = None) -> int:
        """Count unread messages without retaining their bodies."""
        counts = self.pending_counts(name)
        if target is None or BuiltinChannel.aggregate_target(target):
            return sum(counts.values())
        if is_channel_target(target):
            targets = self._channels.read().history_targets(target)
            return sum(
                count for scope, count in counts.items() if targets is None or scope in targets
            )
        return counts.get(self._registry.require(target).name, 0)

    def pending_counts(self, name: str) -> Mapping[str, int]:
        return self._pending_projection([name])[name]

    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        return {
            name: sum(counts.values()) for name, counts in self._pending_projection(names).items()
        }

    def _pending_projection(self, names: Sequence[str]) -> Mapping[str, Mapping[str, int]]:
        """Capture one registry/read snapshot and sync the route index once."""
        snapshot = self._registry.snapshot()
        catalog = self._channels.read()
        actors = {name: snapshot.canonical_name(name) for name in names}
        deliveries = {}
        for actor in set(actors.values()):
            deliveries[actor] = self._delivery_scope(actor, snapshot, catalog=catalog)
        revision = tuple(
            file_revision(path)
            for path in (
                self.log.path,
                self._registry.store.path,
                self._channels.path,
                self.reads.path,
            )
        )
        if all(
            name in self._pending_cache
            and self._pending_cache[name].revision == revision
            and self._pending_cache[name].delivery == deliveries[actor]
            for name, actor in actors.items()
        ):
            return {name: dict(self._pending_cache[name].counts) for name in names}
        document = self.reads.read()
        seen = {
            actor: self.reads.seen_sequences(actor, snapshot, document=document)
            for actor in deliveries
        }
        with self.log._opened_wire_snapshot(need_sequence=False) as source:
            counts = self._pending_counts(source, snapshot, deliveries, seen)
        for name, actor in actors.items():
            self._pending_cache[name] = PendingCounts(revision, deliveries[actor], counts[actor])
        return {name: dict(counts[actor]) for name, actor in actors.items()}

    def pending_counts_opened(self, source: OpenedWireSnapshot,
                              selection: DisplaySelection) -> Mapping[str, int]:
        """Keep delivery semantics, using the caller's original bus/identity/read cut."""
        actor = selection.viewer
        if actor is None:
            raise ValueError("Delivery projection requires an acquired viewer")
        delivery = self._delivery_scope(actor, selection.registry, catalog=selection.catalog)
        return self._pending_counts(source, selection.registry, {actor: delivery},
                                    {actor: selection.seen})[actor]

    def _pending_counts(
        self, source: OpenedWireSnapshot, snapshot: RegistrySnapshot,
        deliveries: Mapping[str, DeliveryScope], seen: Mapping[str, frozenset[int]],
    ) -> dict[str, dict[str, int]]:
        channel_members: dict[str, set[str]] = {}
        for actor, delivery in deliveries.items():
            for target in delivery.channels:
                channel_members.setdefault(target, set()).add(actor)
        counts: dict[str, dict[str, int]] = {actor: {} for actor in deliveries}

        def recipients(target: str):
            direct = snapshot.canonical_name(target)
            return channel_members.get(target, {direct} if direct in deliveries else set())

        def add(actor: str, sender: str, target: str, count: int):
            if count:
                conversation = deliveries[actor].conversation(sender, target)
                counts[actor][conversation] = counts[actor].get(conversation, 0) + count

        indexed = False
        from functools import partial

        decode = partial(DeliveryMessage.from_wire, root_id=source.metadata.root_id)
        try:
            with BusRouteCounts(self.log.path) as index:
                if index.sync(source.stream, source.revision, decode):
                    requests = []
                    for route in index.routes():
                        for actor in recipients(route.target):
                            since = deliveries[actor].minimum_timestamp(
                                route.sender, route.target, route.sender_lookup, snapshot
                            )
                            if since is not None:
                                requests.append(PendingRoute(
                                    actor, route.target, route.sender, route.sender_lookup, since,
                                ))
                    for row in index.unseen_counts(
                        requests,
                        [ActorSeen(actor, sequences) for actor, sequences in seen.items()],
                    ):
                        add(row.actor, row.sender, row.target, row.count)
                    indexed = True
        except (OSError, sqlite3.DatabaseError):
            # A disposable index outage still uses exactly the same identity filter.
            pass
        if not indexed:
            if source.stream is not None:
                source.stream.seek(0)
            records = self.log._snapshot_records(source.metadata, source.stream, source.boundary)
            for item in (item for record in records for item in record.delivery_messages()):
                message = item.message
                for actor in recipients(message.target):
                    if deliveries[actor].current(item, snapshot) and message.seq not in seen[actor]:
                        add(actor, message.sender, message.target, 1)
        return counts

    def mark_delivered(self, name: str, target: str | None = None) -> int:
        """Mark unread messages delivered and return the count without retaining them."""
        messages = self.inbox(name, target)
        basis = self.reads.capture(name, messages, self._registry.snapshot(), self.log.path)
        self.reads.mark_displayed(basis.viewer, basis)
        return len(messages)

    def mark_delivered_through(self, name: str, sequence: int) -> None:
        """Advance a thread's global inbox cursor without loading messages."""
        canonical = self._registry.require(name).name
        if sequence < 0:
            raise ValueError("Delivery sequence cannot be negative.")
        messages = (message for message in self.inbox(canonical) if message.seq <= sequence)
        displayed = self.reads.capture(
            canonical, messages, self._registry.snapshot(), self.log.path
        )
        self.reads.mark_displayed(canonical, displayed)
        return

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, in seq order."""
        scope = DMDisplayScope.capture(
            self._registry.require(a).name, self._registry.require(b).name,
            self._registry.snapshot(),
        )
        return [message for message in self.log.full_history() if scope.includes(message)]

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full target-owned channel history, independent of delivery membership."""
        if not is_channel_target(target):
            raise ValueError(f"{target!r} is not a channel target.")
        scope = ChannelDisplayScope(target, self._channels.read().history_targets(target))
        return [message for message in self.log.full_history() if scope.includes(message)]

    def awareness_segments(self, owner: Thread):
        """Bounded pointers to addressed sources, independent of UI read state.

        Current canonical checkpoint rows own the pointers. Repeated reminders
        are intentional: neither displaying nor composing them proves model read.
        """
        from .bus_publication import stable_thread_lookup
        from .errors import RelationViolationError
        from .private_bus_checkpoint import addressed_source_pointers_unlocked
        from .context_segments.awareness import AwarenessSegment, UnavailableAwarenessSegment

        try:
            with self.log.locked(blocking=False):
                marker = self.log._private_marker_unlocked()
                rows = addressed_source_pointers_unlocked(
                    self.log, marker, stable_thread_lookup(owner.created_at)
                )
        except (OSError, ValueError, sqlite3.Error, RelationViolationError):
            return (UnavailableAwarenessSegment.capture(),)
        if not rows:
            return ()
        return (AwarenessSegment.capture(self.log.path, marker.root_id, rows),)

    def incoming_page(self, name: str, *, after: int, limit: int = 100) -> MessagePage:
        """A bounded delivery stream independent of UI read acknowledgments."""
        thread = self._registry.require(name)
        delivery = self._delivery_scope(thread.name)
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
        """Return one bounded page between two threads in ascending order."""
        a = self._registry.require(a).name
        b = self._registry.require(b).name
        scope = DMDisplayScope.capture(a, b, self._registry.snapshot())
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
