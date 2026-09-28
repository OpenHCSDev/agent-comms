"""Delivery, indexed reads and history over owned wire/publication components."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_activity_index import BusActivityIndex, ChannelActivity
from .bus_page_index import BusPageIndex, StaleBusPageIndexError
from .bus_route_counts import BusRouteCounts
from .channel_targets import _TAG_CHARS, BuiltinChannel, is_channel_target
from .errors import (
    RelationViolationError,
    UnregisteredThreadError,
)
from .field_codec import FieldCodec
from .message_page import MessagePage
from .messages import Message, MessageType
from .read_basis import (
    ChannelDisplayScope,
    DMDisplayScope,
    MessageDisplayScope,
    ViewUnread,
)
from .routing import DeliveryScope, PendingCounts
from .store_files import (
    _atomic_write_text,
    _iter_jsonl_records,
    _store_lock,
    file_revision,
)
from .thread_identity import ThreadRole

if TYPE_CHECKING:
    from .historical_views import HistorySource, HistoryView
    from .registration import Registration

from .publisher import Publisher
from .wire_log import WireLog


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
        self._registry = registry
        self._channels = ChannelCatalog(bus_path.parent / ChannelCatalog.filename)
        self._pending_cache: dict[str, PendingCounts] = {}
        self._view_unread_cache: dict[str, ViewUnread] = {}
        self._channel_activity_revision: tuple | None = None
        self._channel_activity: dict[str, ChannelActivity] = {}
        self.publisher = Publisher(
            self.log,
            registry,
            self._channels,
            private_response_writes=private_response_writes,
            private_initial_writes=private_initial_writes,
            private_claim_writes=private_claim_writes,
        )

    @property
    def history_manifest(self) -> Path:
        return self.log.path.with_name("history_sources.json")

    def history_sources(self) -> tuple[HistorySource, ...]:
        from .historical_views import HistorySource

        try:
            raw = json.loads(self.history_manifest.read_text())
        except FileNotFoundError:
            return ()
        return tuple(FieldCodec.decode(HistorySource, item) for item in raw)

    def attach_history(self, source_root: Path) -> HistorySource:
        """Snapshot a preserved source, then publish it for ordinary display.

        Only destination files are written. No Comms constructor, source locks,
        inboxes, execution inputs, or source sequence allocator are touched.
        A source is attached once. Its original bytes and identity survive.
        """
        import shutil
        import tempfile

        from .catalog_store import ChannelCatalog

        from .historical_views import HistorySource

        source_root = source_root.resolve()
        if source_root == self.log.path.parent.resolve():
            raise ValueError("The live bus cannot be its own history source")
        with _store_lock(self.history_manifest):
            sources = self.history_sources()
            existing = next((s for s in sources if s.original_root == str(source_root)), None)
            if existing is not None:
                return existing
            parent = self.log.path.parent / "history"
            parent.mkdir(mode=0o700, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix="source-", dir=parent))
            try:
                paths = [
                    source_root / name
                    for name in (
                        "bus.jsonl",
                        "registry.json",
                        ChannelCatalog.filename,
                        "transcript_routes.json",
                        "bus_meta.json",
                    )
                ]
                revisions = tuple(file_revision(path) for path in paths)
                for path in paths:
                    if path.exists():
                        shutil.copyfile(path, stage / path.name)
                if revisions != tuple(file_revision(path) for path in paths):
                    raise ValueError("Historical source changed during snapshot; retry")
                bus_info = paths[0].stat() if paths[0].exists() else None
                if bus_info is None:
                    (stage / "bus.jsonl").touch()
                # Source metadata is provenance only. Never turn the snapshot
                # into an active private root or copy coordinator/native state.
                meta = stage / "bus_meta.json"
                marker = json.loads(meta.read_text()) if meta.exists() else {}
                if meta.exists():
                    meta.rename(stage / "source_bus_meta.json")
                source = HistorySource(
                    str(stage.resolve()),
                    str(source_root),
                    marker.get("wire_root_id", ""),
                    (bus_info.st_dev, bus_info.st_ino) if bus_info else (0, 0),
                    bus_info.st_size if bus_info else 0,
                    file_revision(stage / "bus.jsonl"),
                    file_revision(stage / "registry.json"),
                )
                registry = source.registry().snapshot()
                previous = 0
                for record, size in _iter_jsonl_records(stage / "bus.jsonl"):
                    message, _ = self.log._public_page_record(record, size)
                    if message.seq <= previous:
                        raise ValueError("Historical source has nonascending sequences")
                    previous = message.seq
                if not registry.threads:
                    raise ValueError("Historical source has no identity declarations")
                _atomic_write_text(
                    self.history_manifest,
                    json.dumps([FieldCodec.encode(item) for item in (*sources, source)]),
                )
                return source
            except BaseException:
                shutil.rmtree(stage)
                raise

    def historical_page(
        self, view: HistoryView, *, before=None, after=None, limit=100, max_bytes=256 * 1024
    ):
        """Page one original source at a time, with source-bound cursors.

        The caller owns live pages. None means the oldest live boundary;
        a historical cursor can travel in either direction across snapshots.
        """
        from .historical_views import HistoricalMessage

        sources = self.history_sources()
        cursor = before or after
        if before is not None and after is not None:
            raise ValueError("Choose one history paging direction")
        start = next(
            (i for i, source in enumerate(sources) if cursor and source.key == cursor.source),
            len(sources) - 1 if cursor is None else -1,
        )
        if cursor is not None and start < 0:
            raise ValueError("Historical source detached; reload history")
        indexes = range(start, len(sources)) if after else range(start, -1, -1)
        for index in indexes:
            source = sources[index]
            registry = source.registry()
            snapshot = registry.snapshot()
            source.validate()
            scope = view.capture(snapshot)
            historical_bus = MessageBus(Path(source.root) / "bus.jsonl", registry)
            page = historical_bus.display_page(
                scope,
                before=cursor.sequence if before and index == start else None,
                after=cursor.sequence if after and index == start else (0 if after else None),
                limit=limit,
                max_bytes=max_bytes,
            )

            page = replace(
                page,
                messages=tuple(
                    HistoricalMessage.project(message, source, index, snapshot)
                    for message in page.messages
                ),
            )
            if page.messages:
                # Cross-source availability is resolved by the next bounded read;
                # false-positive edges terminate on an empty page without replay.
                return replace(
                    page,
                    has_older=page.has_older or index > 0,
                    has_newer=page.has_newer or index < len(sources) - 1,
                )
        return MessagePage((), False, False)

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

    def channel_activity(self) -> Mapping[str, ChannelActivity]:
        """Aggregate channel history clocks once per wire revision, not per viewer."""
        with self.log.locked():
            revision = file_revision(self.log.path)
            if revision != self._channel_activity_revision:
                projection = BusActivityIndex(self.log.path).snapshot(
                    revision, self._bus_activity_fields
                )
                if projection is None:
                    activity: dict[str, ChannelActivity] = {}
                    for message in self.log._iter_log_unlocked():
                        activity[message.target] = activity.get(
                            message.target, ChannelActivity()
                        ).observe(message)
                else:
                    channels, _ = projection
                    activity = {
                        target: ChannelActivity(last_message, last_user)
                        for target, (last_message, last_user) in channels.items()
                    }
                self._channel_activity = activity
                self._channel_activity_revision = revision
            return dict(self._channel_activity)

    @staticmethod
    def _bus_activity_fields(record: Mapping[str, object]) -> tuple[str, str, float, bool, bool]:
        message = Message.from_wire(record)
        return (
            message.sender,
            message.target,
            message.timestamp,
            not message.sender_role.executable,
            message.membership is None and not message.notice,
        )

    def _delivery_scope(self, name: str) -> DeliveryScope:
        snapshot = self._registry.snapshot()
        canonical = snapshot.aliases.get(name, name)
        if canonical not in snapshot.threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        thread = snapshot.threads[canonical]
        return DeliveryScope(
            thread.name, snapshot.aliases, self._channels.read().targets_for(thread.tags)
        )

    def inbox(self, name: str, target: str | None = None) -> Sequence[Message]:
        delivery = self._delivery_scope(name)
        name = delivery.actor
        matches = self._scope_filter(delivery, target)
        seen = self.reads.seen_sequences(name, self._registry.snapshot())
        return [
            message
            for message in self.log.full_history()
            if delivery.delivers(message.sender, message.target)
            and matches(message)
            and message.seq not in seen
        ]

    def pending_count(self, name: str, target: str | None = None) -> int:
        """Count unread messages without retaining their bodies."""
        counts = self.pending_counts(name)
        if target is None or BuiltinChannel.aggregate_target(target):
            return sum(counts.values())
        if is_channel_target(target) or BuiltinChannel.is_alias(target):
            targets = self._channels.read().history_targets(target)
            return sum(
                count for scope, count in counts.items() if targets is None or scope in targets
            )
        return counts.get(self._registry.require(target).name, 0)

    def _scope_filter(
        self, delivery: DeliveryScope, target: str | None
    ) -> Callable[[Message], bool]:
        if target is None:
            return lambda message: True
        if is_channel_target(target) or BuiltinChannel.is_alias(target):
            targets = self._channels.read().history_targets(target)
            return lambda message: targets is None or message.target in targets
        peer = self._registry.require(target).name
        return lambda message: delivery.conversation(message.sender, message.target) == peer

    def pending_counts(self, name: str) -> Mapping[str, int]:
        """Count one thread's unread messages by conversation in one log pass."""
        delivery = self._delivery_scope(name)
        revision = tuple(
            file_revision(path)
            for path in (
                self.log.path,
                self._registry.store.path,
                self._channels.path,
                self.reads.path,
            )
        )
        cached = self._pending_cache.get(name)
        if cached is not None and cached.revision == revision and cached.delivery == delivery:
            return dict(cached.counts)
        seen = self.reads.seen_sequences(delivery.actor, self._registry.snapshot())
        counts: dict[str, int] = {}
        with self.log.locked():
            try:
                with BusRouteCounts(self.log.path) as route_counts:
                    if route_counts.sync(self._pending_route_fields):
                        for target, sender, unread in route_counts.unseen_counts(seen):
                            if delivery.delivers(sender, target):
                                conversation = delivery.conversation(sender, target)
                                counts[conversation] = counts.get(conversation, 0) + unread
                        self._pending_cache[name] = PendingCounts(revision, delivery, counts)
                        return dict(counts)
            except (OSError, sqlite3.DatabaseError):
                # The index is disposable; exact ledger membership still owns unread.
                pass
            for message in self.log._iter_log_unlocked():
                if delivery.delivers(message.sender, message.target) and message.seq not in seen:
                    conversation = delivery.conversation(message.sender, message.target)
                    counts[conversation] = counts.get(conversation, 0) + 1
        self._pending_cache[name] = PendingCounts(revision, delivery, counts)
        return dict(counts)

    @staticmethod
    def _pending_route_fields(record: Mapping) -> tuple[int, str, str]:
        """Validate ordinary wire routing fields without constructing a Message.

        Rich records retain Message.from_wire's complete validation, including
        mention offsets and claim-transition binding. An invalid plain row is
        never silently skipped or treated as a zero pending count.
        """
        if any(key in record for key in ("membership", "mentions", "claim_transition")):
            message = Message.from_wire(record)
            return message.seq, message.sender, message.target
        sender, target, body = record["from"], record["to"], record["text"]
        seq = int(record.get("seq", 0))
        MessageType(record["type"])
        ThreadRole(record.get("sender_role", ThreadRole.AGENT.value))
        if not isinstance(sender, str) or not sender:
            raise RelationViolationError("Message sender cannot be empty.")
        if not isinstance(target, str) or not target:
            raise RelationViolationError("Message target cannot be empty.")
        if not isinstance(body, str) or not body:
            raise ValueError("Message body cannot be empty.")
        if not BuiltinChannel.is_alias(target) and not is_channel_target(target):
            if sender == target:
                raise RelationViolationError(f"Thread {sender!r} cannot message itself.")
            allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
            if not set(target) <= allowed:
                raise ValueError(f"Message target {target!r} is not a thread name or #channel.")
        if target.startswith("#") and target != BuiltinChannel.ALL.value:
            tag = target[1:]
            if not tag or not set(tag) <= _TAG_CHARS:
                raise ValueError(f"Channel {target!r} has an invalid tag.")
        return seq, sender, target

    def _iter_pending_routes_unlocked(self) -> Iterator[tuple[int, str, str]]:
        for record, _ in _iter_jsonl_records(self.log.path):
            yield self._pending_route_fields(record)

    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        """Count every requested inbox from one wire and read-ledger snapshot."""
        snapshot = self._registry.snapshot()
        catalog = self._channels.read()
        document = self.reads.read()
        actors = {name: snapshot.aliases.get(name, name) for name in names}
        deliveries = {}
        seen = {}
        channel_members: dict[str, set[str]] = {}
        for actor in set(actors.values()):
            if actor not in snapshot.threads:
                raise UnregisteredThreadError(f"Thread {actor!r} is not registered.")
            owner = snapshot.threads[actor]
            deliveries[actor] = DeliveryScope(
                actor, snapshot.aliases, catalog.targets_for(owner.tags)
            )
            seen[actor] = self.reads.seen_sequences(actor, snapshot, document=document)
            for target in deliveries[actor].channels:
                channel_members.setdefault(target, set()).add(actor)
        counts = dict.fromkeys(deliveries, 0)
        with self.log.locked():
            for sequence, raw_sender, target in self._iter_pending_routes_unlocked():
                sender = snapshot.aliases.get(raw_sender, raw_sender)
                direct = snapshot.aliases.get(target, target)
                recipients = channel_members.get(
                    target, {direct} if direct in deliveries else set()
                )
                for actor in recipients:
                    if actor != sender and sequence not in seen[actor]:
                        counts[actor] += 1
        return {name: counts[actor] for name, actor in actors.items()}

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
        a = self._registry.require(a).name
        b = self._registry.require(b).name
        return [
            msg
            for msg in self.log.full_history()
            if {
                self._registry.canonical_name(msg.sender),
                self._registry.canonical_name(msg.target),
            }
            == {a, b}
        ]

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full history of one channel (``#all`` or a tag channel)."""
        if not (is_channel_target(target) or BuiltinChannel.is_alias(target)):
            raise ValueError(f"{target!r} is not a channel target.")
        targets = self._channels.read().history_targets(target)
        return [msg for msg in self.log.full_history() if targets is None or msg.target in targets]

    def incoming_page(self, name: str, *, after: int, limit: int = 100) -> MessagePage:
        """A bounded delivery stream independent of UI read acknowledgments."""
        thread = self._registry.require(name)
        delivery = self._delivery_scope(thread.name)
        return self._history_page(
            lambda message: delivery.delivers(message.sender, message.target),
            before=None,
            after=after,
            limit=limit,
            max_bytes=256 * 1024,
            targets=frozenset(delivery.channels | self._registry.aliases_for(delivery.actor)),
        )

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
        if not (is_channel_target(target) or BuiltinChannel.is_alias(target)):
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
        return self._history_page(
            scope.includes,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
            targets=scope.index_targets,
        )

    def _history_page(
        self,
        matches: Callable[[Message], bool],
        *,
        before: int | None,
        after: int | None,
        limit: int,
        max_bytes: int,
        targets: frozenset[str] | None = None,
    ) -> MessagePage:
        with self.log.locked():
            try:
                with BusPageIndex(self.log.path) as index:
                    if index.sync():
                        return self._indexed_history_page(
                            index,
                            matches,
                            before=before,
                            after=after,
                            limit=limit,
                            max_bytes=max_bytes,
                            targets=targets,
                        )
            except (OSError, sqlite3.DatabaseError, StaleBusPageIndexError):
                # The JSONL bus remains authoritative if the disposable
                # index is unavailable or its selected offsets disagree.
                pass
            return self._collect_history_page(
                (
                    self.log._public_page_record(record, size)
                    for record, size in _iter_jsonl_records(self.log.path)
                ),
                matches,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )

    def _indexed_history_page(
        self,
        index: BusPageIndex,
        matches: Callable[[Message], bool],
        *,
        before: int | None,
        after: int | None,
        limit: int,
        max_bytes: int,
        targets: frozenset[str] | None,
    ) -> MessagePage:
        if before is not None and after is not None:
            raise ValueError("History pages accept either before or after, not both.")
        if (before is not None and before < 0) or (after is not None and after < 0):
            raise ValueError("History cursors cannot be negative.")
        if limit <= 0:
            raise ValueError("History page limit must be positive.")
        if max_bytes <= 0:
            raise ValueError("History page byte budget must be positive.")
        page: deque[tuple[Message, int]] = deque()
        page_bytes = 0
        with self.log.path.open("rb") as stream:

            def has_match(*, lower: int | None, upper: int | None) -> bool:
                with closing(
                    index.offsets(lower=lower, upper=upper, descending=True, targets=targets)
                ) as rows:
                    for row in rows:
                        message, _ = self.log._public_page_record(*index.record(stream, row))
                        if matches(message):
                            return True
                return False

            if after is not None:
                has_older = has_match(lower=None, upper=after + 1)
                has_newer = False
                rows = index.offsets(lower=after, upper=None, descending=False, targets=targets)
            else:
                has_older = False
                has_newer = has_match(lower=before - 1, upper=None) if before is not None else False
                rows = index.offsets(lower=None, upper=before, descending=True, targets=targets)
            with closing(rows):
                for row in rows:
                    message, encoded_size = self.log._public_page_record(*index.record(stream, row))
                    if not matches(message):
                        continue
                    if len(page) >= limit or (page and page_bytes + encoded_size > max_bytes):
                        if after is not None:
                            has_newer = True
                        else:
                            has_older = True
                        break
                    if after is not None:
                        page.append((message, encoded_size))
                    else:
                        page.appendleft((message, encoded_size))
                    page_bytes += encoded_size
        return MessagePage(
            messages=tuple(message for message, _ in page),
            has_older=has_older,
            has_newer=has_newer,
        )

    @staticmethod
    def _collect_history_page(
        records: Iterator[tuple[Message, int]],
        matches: Callable[[Message], bool],
        *,
        before: int | None,
        after: int | None,
        limit: int,
        max_bytes: int,
    ) -> MessagePage:
        if before is not None and after is not None:
            raise ValueError("History pages accept either before or after, not both.")
        if (before is not None and before < 0) or (after is not None and after < 0):
            raise ValueError("History cursors cannot be negative.")
        if limit <= 0:
            raise ValueError("History page limit must be positive.")
        if max_bytes <= 0:
            raise ValueError("History page byte budget must be positive.")
        page: deque[tuple[Message, int]] = deque()
        page_bytes = 0
        has_older = has_newer = False
        for message, encoded_size in records:
            if not matches(message):
                continue
            if before is not None and message.seq >= before:
                has_newer = True
                continue
            if after is not None and message.seq <= after:
                has_older = True
                continue
            if after is not None:
                if len(page) >= limit or (page and page_bytes + encoded_size > max_bytes):
                    has_newer = True
                    # Forward cursors must never jump over an eligible row.
                    # Leave this row for the next page, even if a later,
                    # smaller row would fit in the remaining byte budget.
                    break
                page.append((message, encoded_size))
                page_bytes += encoded_size
                continue
            page.append((message, encoded_size))
            page_bytes += encoded_size
            while len(page) > limit or (len(page) > 1 and page_bytes > max_bytes):
                _, removed_size = page.popleft()
                page_bytes -= removed_size
                has_older = True
        return MessagePage(
            messages=tuple(message for message, _ in page),
            has_older=has_older,
            has_newer=has_newer,
        )

    def last_sent_timestamps(self) -> Mapping[str, float]:
        """Aggregate sent times without retaining message bodies."""
        with self.log.locked():
            projection = BusActivityIndex(self.log.path).snapshot(
                file_revision(self.log.path), self._bus_activity_fields
            )
            if projection is not None:
                return projection[1]
            latest: dict[str, float] = {}
            for message in self.log._iter_log_unlocked():
                if message.membership is None and not message.notice:
                    latest[message.sender] = max(latest.get(message.sender, 0.0), message.timestamp)
        return latest

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
