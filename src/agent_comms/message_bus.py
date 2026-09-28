"""Message bus: declaration and persistence owners."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from .bus_activity_index import BusActivityIndex, ChannelActivity
from .bus_durability import _claim_gate_enabled, _claim_gate_path
from .bus_page_index import BusPageIndex, StaleBusPageIndexError
from .bus_publication import (
    PRIVATE_WIRE_FIELD,
    CommittedInitial,
    HumanOrigin,
    has_private_wire_fields,
    initial_sideband,
    public_envelope_digest,
    stable_thread_lookup,
    unique_wire_object,
    validate_initial_record,
)
from .bus_route_counts import BusRouteCounts
from .channel_targets import _TAG_CHARS, GLOBAL_CHANNEL, BuiltinChannel, is_channel_target
from .channels import Channel
from .envelope_claim_transitions import (
    ClaimProjection,
    ClaimRelease,
    ClaimTransition,
    FileClaimPath,
    WakeAdmission,
    _claim_transition_from_wire,
    _claim_transition_wire,
    _release_resource,
    apply_transition,
    normalize_claim_file,
)
from .errors import (
    ClaimEnvelopeUnknownError,
    HumanInitialUnknownError,
    RelationViolationError,
    UnregisteredThreadError,
)
from .field_codec import FieldCodec
from .mentions import ThreadMention
from .message_page import MessagePage
from .messages import Message, MessageType
from .private_registry_guard import _require_no_private_owner_rename
from .read_basis import ChannelDisplayScope, DisplayBasis, ViewUnread
from .registry_document import RegistrySnapshot
from .routing import DeliveryScope, PendingCounts
from .store_files import (
    _append_jsonl,
    _atomic_write_text,
    _iter_jsonl_records,
    _iter_jsonl_stream,
    _repair_trailing_jsonl,
    _store_lock,
    file_revision,
)
from .thread_identity import ThreadRole

if TYPE_CHECKING:
    from .coordination import PublicationIntent
    from .historical_views import HistorySource
    from .registration import Registration


class MessageBus:
    """Routes messages between registered threads."""

    def __init__(
        self,
        bus_path: Path,
        registry: Registration,
        *,
        private_response_writes: bool = False,
        private_initial_writes: bool = False,
        private_claim_writes: bool = False,
    ):
        from .channels import ChannelCatalog
        from .read_ledger import ReadLedger

        self.reads = ReadLedger(bus_path.parent / ReadLedger.filename)
        self._path = bus_path
        self._registry = registry
        self._private_response_writes = private_response_writes
        self._private_initial_writes = private_initial_writes
        self._private_claim_writes = private_claim_writes
        self._channels = ChannelCatalog(bus_path.parent / "channels.json", registry)
        self._pending_cache: dict[str, PendingCounts] = {}
        self._view_unread_cache: dict[str, ViewUnread] = {}
        self._channel_activity_revision: tuple | None = None
        self._channel_activity: dict[str, ChannelActivity] = {}

    @property
    def history_manifest(self) -> Path:
        return self._path.with_name("history_sources.json")

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

        from .historical_views import HistorySource

        source_root = source_root.resolve()
        if source_root == self._path.parent.resolve():
            raise ValueError("The live bus cannot be its own history source")
        with _store_lock(self.history_manifest):
            sources = self.history_sources()
            existing = next((s for s in sources if s.original_root == str(source_root)), None)
            if existing is not None:
                return existing
            parent = self._path.parent / "history"
            parent.mkdir(mode=0o700, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix="source-", dir=parent))
            try:
                paths = [
                    source_root / name
                    for name in (
                        "bus.jsonl",
                        "registry.json",
                        "channels.json",
                        "channel_metadata.json",
                        "transcript_routes.json",
                        "bus_meta.json",
                        "channel_pins.json",
                        "saved_views.json",
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
                    message, _ = self._public_page_record(record, size)
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

    def historical_page(self, matches, *, before=None, after=None, limit=100, max_bytes=256 * 1024):
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
            snapshot = source.registry().snapshot()
            historical_bus = MessageBus(Path(source.root) / "bus.jsonl", source.registry())
            page = historical_bus._history_page(
                lambda message, snapshot=snapshot: matches(message, snapshot),
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
            scopes = tuple(
                ChannelDisplayScope(
                    channel.name, self._channels.history_targets(channel.name), seen_sequences=seen
                )
                for channel in self._channels.views().values()
            )
        else:
            scopes = display_scopes
        revision = (file_revision(self._path), viewer_names)
        cached = self._view_unread_cache.get(viewer)
        if cached is not None and cached.revision == revision and cached.scopes == scopes:
            return dict(cached.counts)
        counts = dict.fromkeys((scope.channel for scope in scopes), 0)
        with self._record_snapshot() as (_, records):
            for message, _ in records:
                if message.sender in (viewer_names if viewer_names is not None else {viewer}):
                    continue
                for scope in scopes:
                    if scope.unread(message):
                        counts[scope.channel] += 1
        self._view_unread_cache[viewer] = ViewUnread(revision, scopes, counts)
        return dict(counts)

    def mark_view_read(
        self, viewer: str, target: str, through: int, *, displayed: DisplayBasis
    ) -> None:
        """Compatibility entry point; only an actual display basis advances reads."""
        self.reads.mark_displayed(viewer, displayed.through(through))

    def channel_activity(self) -> Mapping[str, ChannelActivity]:
        """Aggregate channel history clocks once per wire revision, not per viewer."""
        with _store_lock(self._path):
            revision = file_revision(self._path)
            if revision != self._channel_activity_revision:
                projection = BusActivityIndex(self._path).snapshot(
                    revision, self._bus_activity_fields
                )
                if projection is None:
                    activity: dict[str, ChannelActivity] = {}
                    for message in self._iter_log_unlocked():
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
        return DeliveryScope(thread.name, snapshot.aliases, self._channels.targets_for(thread.tags))

    def send(self, message: Message) -> str:
        return self.publish(message).message_id

    def _validate_publish_request(
        self, message: Message, *, registry_snapshot: RegistrySnapshot | None = None
    ) -> tuple[str, str]:
        """Resolve the public route, optionally using a locked registry revision."""

        def canonical(name: str) -> str:
            if registry_snapshot is None:
                return self._registry.canonical_name(name)
            return registry_snapshot.aliases.get(name, name)

        def exists(name: str) -> bool:
            if registry_snapshot is None:
                return name in self._registry
            return canonical(name) in registry_snapshot.threads

        if not exists(message.sender):
            raise UnregisteredThreadError(f"Sender {message.sender!r} is not a registered thread.")
        if BuiltinChannel.aggregate_target(message.target) or (
            is_channel_target(message.target) and self._channels.is_view_target(message.target)
        ):
            raise RelationViolationError(
                f"View {message.target!r} is a projection, not a routable target."
            )
        if (
            not is_channel_target(message.target)
            and not BuiltinChannel.is_alias(message.target)
            and not exists(message.target)
        ):
            raise UnregisteredThreadError(f"Target {message.target!r} is not a registered thread.")
        sender = canonical(message.sender)
        target = message.target
        if (
            not is_channel_target(target)
            and not BuiltinChannel.is_alias(target)
            and canonical(target) == sender
        ):
            raise RelationViolationError(f"Thread {sender!r} cannot message itself.")
        return sender, target

    def _prepare_message_unlocked(
        self,
        message: Message,
        *,
        sender: str,
        target: str,
        sequence: int,
        snapshot: RegistrySnapshot | None = None,
    ) -> Message:
        snapshot = snapshot or self._registry.snapshot()

        def resolve_mention(name: str) -> str | None:
            canonical = snapshot.aliases.get(name, name)
            thread = snapshot.threads.get(canonical)
            if (
                thread is not None
                and thread.role.executable
                and snapshot.statuses[canonical].visible
            ):
                return canonical
            return None

        return replace(
            message,
            sender=sender,
            target=BuiltinChannel.canonical(target),
            seq=sequence,
            sender_role=snapshot.threads[sender].role,
            mentions=ThreadMention.find(message.body, resolve_mention),
        )

    def publish(self, message: Message) -> Message:
        """Commit an ordinary row; refuse legacy appends after private cutover."""
        from .active_route import guard_legacy_root_write

        with guard_legacy_root_write(self._path.parent):
            return self._publish_legacy(message)

    def _publish_legacy(self, message: Message) -> Message:
        if message.claim_transition is not None:
            raise RelationViolationError("Claim envelopes require the gated private sender.")
        sender, target = self._validate_publish_request(message)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            sequence_path = self._path.parent / "bus_meta.json"
            metadata = json.loads(sequence_path.read_text()) if sequence_path.exists() else {}
            if not isinstance(metadata, dict) or "writer_protocol_version" in metadata:
                raise RelationViolationError("Legacy append is unavailable after private cutover.")
            # A crash may leave a valid final row without its newline. Complete
            # it before choosing a sequence, as the append path would do later.
            _repair_trailing_jsonl(self._path)
            last_sequence = max(
                self._read_last_sequence(sequence_path), self._last_row_sequence_unlocked()
            )
            stored = self._prepare_message_unlocked(
                message, sender=sender, target=target, sequence=last_sequence + 1
            )
            _atomic_write_text(
                sequence_path, json.dumps({"last_seq": stored.seq}, indent=2), fsync_parent=True
            )
            _append_jsonl(self._path, stored.to_wire())
        return stored

    def publish_ordinary(
        self, message: Message, *, _human_origin: HumanOrigin | None = None
    ) -> Message:
        """Ordinary Comms send on either a legacy or explicitly marked private root.

        A private marker is never installed here and an old public row is never
        retroactively assigned an audience. Only the private-aware writer uses
        this entry point: direct legacy ``publish`` still refuses cutover. A
        typed local USER origin is valid only at the explicit human operation;
        generic private publication still rejects USER senders.
        The caller retains the ordinary Comms wire lock throughout publication.
        """
        if _human_origin is not None and type(_human_origin) is not HumanOrigin:
            raise RelationViolationError("Human origin must be a typed local USER identity.")
        with _store_lock(self._path):
            meta = self._path.parent / "bus_meta.json"
            metadata = json.loads(meta.read_text()) if meta.exists() else {}
            if not isinstance(metadata, dict):
                raise RelationViolationError("Invalid ordinary delivery metadata.")
            if "writer_protocol_version" in metadata:
                # Exact marker/root/private-registry validation and frozen N/K
                # decisions remain owned by the existing private publisher.
                return self.publish_initial_cohort(
                    message, _bus_locked=True, _human_origin=_human_origin
                )
        # Legacy publish rechecks its barrier under its own lock: if a fresh-root
        # cutover raced the dispatch, it refuses rather than appending a legacy row.
        return self.publish(message)

    def _assert_private_directory(self) -> None:
        """Require a nonredirectable, owned ancestry (root sticky /tmp permitted)."""
        if os.name != "posix":
            raise RelationViolationError("Private bus requires POSIX ownership and modes.")
        # Walk the lexical absolute spelling, not only a relative root up to
        # Path('.'); resolve() would hide symlink ancestors instead of rejecting them.
        if ".." in self._path.parts:
            raise RelationViolationError("Private bus directory ancestry is not trusted.")
        path = self._path.parent.absolute()
        while True:
            info = path.lstat()
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or (info.st_mode & 0o022 and not sticky_root)
            ):
                raise RelationViolationError("Private bus directory ancestry is not trusted.")
            if path == path.parent:
                break
            path = path.parent

    def initialize_private_protocol(self) -> str:
        """Marker issuer for a NEW, isolated bus root only.

        Operational old-writer quiescence remains required for any future live
        cutover; this issuer refuses a legacy log rather than guessing it.
        """
        if self._private_initial_writes is not True:
            raise RelationViolationError("Private initial publication is disabled.")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            self._assert_private_directory()
            root_info = self._path.parent.lstat()
            if root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) != 0o700:
                raise RelationViolationError("Private bus directory must be owner-only.")
            meta = self._path.parent / "bus_meta.json"
            repair = self._path.with_name(self._path.name + ".corrupt")
            if any(path.exists() or path.is_symlink() for path in (meta, self._path, repair)):
                raise RelationViolationError("Private marker issuer requires a fresh bus root.")
            root_id = uuid.uuid4().hex
            from .private_registry_guard import PrivateRegistryGuard

            # Total order: caller's wire lock, bus lock, registry lock. The
            # durable PENDING guard precedes marker visibility; a failed
            # marker/directory fsync cannot leave a usable registry witness.
            if self._registry.store.path.parent != self._path.parent:
                raise RelationViolationError("Private registry must share the bus root")
            with _store_lock(self._registry.store.path):
                guard = PrivateRegistryGuard(self._registry.store.path, root_id)
                guard.create_pending()
                _atomic_write_text(
                    meta,
                    json.dumps(
                        {"last_seq": 0, "writer_protocol_version": 1, "wire_root_id": root_id},
                        indent=2,
                    ),
                    fsync_parent=True,
                )
                guard.commit_initial()
                return root_id

    def initialize_private_claim_protocol(self) -> str:
        """Claim gate for a NEW marked root, before ANY bus message exists.

        The version flag in the EXISTING private bus marker is only a read
        barrier. Claim ownership lives in one bus envelope, not in metadata
        or an O_EXCL claim sidecar.
        """
        if not self._private_claim_writes:
            raise RelationViolationError("Claim envelope publication is disabled.")
        if self._path.name != "bus.jsonl":
            raise RelationViolationError("Claim envelope publication requires the canonical bus.")
        with _store_lock(self._path):
            metadata = self._private_marker_unlocked()
            root_id = str(metadata["wire_root_id"])
            if metadata.get("claim_envelopes_version") == 1:
                return root_id
            if int(metadata["last_seq"]) or (self._path.exists() and self._path.stat().st_size):
                raise RelationViolationError("Claim read barrier requires an empty private bus.")
            metadata["claim_envelopes_version"] = 1
            _atomic_write_text(
                _claim_gate_path(self._path), json.dumps(metadata, indent=2), fsync_parent=True
            )
            return root_id

    def publish_claim_envelope(
        self,
        message: Message,
        *,
        worktree: Path,
        incarnation: str,
        claims: Sequence[str | Path | FileClaimPath] = (),
        releases: Sequence[str | Path] = (),
        _locked_registry_snapshot: RegistrySnapshot | None = None,
        _bus_locked: bool = False,
        _admission: WakeAdmission | None = None,
    ) -> Message:
        """One guarded message and whole-set claim transition in ONE bus row.

        Caller must hold the global Comms wire lock; the bus lock serializes all
        cooperating claim decisions. Failed durability returns UNKNOWN: a later
        guarded reader may re-fsync/adopt a complete visible row, but the caller
        MUST NOT replay its message or provider/tool work automatically.
        """
        from .audience_manifest import MAX_WIRE_SEQ

        if not self._private_claim_writes:
            raise RelationViolationError("Claim envelope publication is disabled.")
        if message.claim_transition is not None or (not claims and not releases):
            raise RelationViolationError("A claim send needs exactly one fresh transition.")
        if (
            isinstance(claims, (str, bytes))
            or isinstance(releases, (str, bytes))
            or not isinstance(claims, Sequence)
            or not isinstance(releases, Sequence)
        ):
            raise RelationViolationError("Claim and release sets must be finite sequences.")
        if len(claims) + len(releases) > 32:
            raise RelationViolationError("Claim envelope exceeds the bounded resource set.")
        if _admission is not None and (
            not claims or releases or not _bus_locked or _locked_registry_snapshot is None
        ):
            raise RelationViolationError("Bound claims require the selected wake boundary.")
        with nullcontext() if _bus_locked else _store_lock(self._path):
            metadata = self._private_marker_unlocked()
            if metadata.get("claim_envelopes_version") != 1:
                raise RelationViolationError("Claim read barrier is unavailable.")
            sender, target = self._validate_publish_request(
                message, registry_snapshot=_locked_registry_snapshot
            )
            projection, verified_sequence = self._claim_projection_unlocked(metadata)
            # The verified bus high-water also covers rows left by an earlier
            # uncertain append. Reserve and sync the next sequence before use.
            last_sequence = max(int(metadata["last_seq"]), verified_sequence)
            if last_sequence >= MAX_WIRE_SEQ:
                raise RelationViolationError("Claim bus sequence is exhausted.")
            stored = self._prepare_message_unlocked(
                message,
                sender=sender,
                target=target,
                sequence=last_sequence + 1,
                snapshot=_locked_registry_snapshot,
            )
            owner_incarnation = str(incarnation)
            requested = tuple(sorted(normalize_claim_file(worktree, path) for path in claims))
            release_paths = tuple(sorted(_release_resource(worktree, path) for path in releases))
            release_records: list[ClaimRelease] = []
            for resource in release_paths:
                current = projection.get(resource)
                if current is None:
                    raise RelationViolationError("Cannot release an unclaimed resource.")
                release_records.append(ClaimRelease(resource, current.generation))
            transition = ClaimTransition(
                stored.sender,
                owner_incarnation,
                stored.seq,
                stored.message_id,
                requested,
                tuple(release_records),
                uuid.uuid4().hex if requested else None,
                _admission,
            )
            # The typed decoder imposes its own bound. Never return success on a
            # durable row that every future guarded reader would reject.
            if _claim_transition_from_wire(_claim_transition_wire(transition)) != transition:
                raise RelationViolationError("Claim transition is not wire-roundtrippable.")
            apply_transition(projection, transition)  # pre-append conflict is synchronous
            stored = replace(stored, claim_transition=transition)
            try:
                self._append_private_unlocked(metadata, stored.to_wire())
            except (OSError, RelationViolationError) as error:
                raise ClaimEnvelopeUnknownError(
                    "Claim envelope outcome UNKNOWN; inspect durable bus; do not replay."
                ) from error
            return stored

    def _claim_projection_unlocked(
        self, metadata: Mapping[str, int | str]
    ) -> tuple[ClaimProjection, int]:
        """Project claims and return the verified bus high-water in one scan."""
        projection = ClaimProjection()
        verified_sequence = 0
        for previous, _, _ in self._verified_private_rows_unlocked(metadata):
            verified_sequence = previous.seq
            if previous.claim_transition is not None:
                projection = apply_transition(projection, previous.claim_transition)
        return projection, verified_sequence

    def claim_projection(self) -> ClaimProjection:
        """Derive ownership exclusively from guarded, verified bus envelopes."""
        with _store_lock(self._path):
            metadata = self._private_marker_unlocked()
            if metadata.get("claim_envelopes_version") != 1:
                raise RelationViolationError("Claim read barrier is unavailable.")
            projection, _verified_sequence = self._claim_projection_unlocked(metadata)
            return projection

    def _private_marker_unlocked(self) -> dict[str, int | str]:
        from .audience_manifest import MAX_WIRE_SEQ

        self._assert_private_directory()
        if self._path.parent.lstat().st_uid != os.geteuid():
            raise RelationViolationError("Private bus directory is not owner-controlled.")
        sequence_path = self._path.parent / "bus_meta.json"
        repair_path = self._path.with_name(self._path.name + ".corrupt")
        for path in (sequence_path, self._path, repair_path):
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise RelationViolationError("Private bus files require regular owner-only mode.")
        if not sequence_path.exists():
            raise RelationViolationError("Private bus writer has no durable protocol marker.")
        try:
            metadata = json.loads(sequence_path.read_text(), object_pairs_hook=unique_wire_object)
        except (ValueError, UnicodeError) as error:
            raise RelationViolationError("Private bus protocol marker is invalid.") from error
        if (
            not isinstance(metadata, dict)
            or set(metadata)
            not in (
                {"last_seq", "writer_protocol_version", "wire_root_id"},
                {"last_seq", "writer_protocol_version", "wire_root_id", "claim_envelopes_version"},
                {
                    "last_seq",
                    "writer_protocol_version",
                    "wire_root_id",
                    "claim_envelopes_version",
                    "checkpoint_version",
                    "checkpoint_seal",
                },
            )
            or (
                "checkpoint_version" in metadata
                and (type(metadata.get("checkpoint_seal")) is not dict)
            )
            or (
                "checkpoint_version" in metadata
                and (
                    type(metadata["checkpoint_version"]) is not int
                    or metadata["checkpoint_version"] != 1
                )
            )
            or (
                "claim_envelopes_version" in metadata
                and (
                    type(metadata["claim_envelopes_version"]) is not int
                    or metadata["claim_envelopes_version"] != 1
                )
            )
            or type(metadata.get("last_seq")) is not int
            or not 0 <= metadata["last_seq"] <= MAX_WIRE_SEQ
            or type(metadata.get("writer_protocol_version")) is not int
            or metadata["writer_protocol_version"] != 1
            or not isinstance(metadata.get("wire_root_id"), str)
            or len(metadata["wire_root_id"]) != 32
            or any(character not in "0123456789abcdef" for character in metadata["wire_root_id"])
        ):
            raise RelationViolationError("Private bus protocol marker is invalid.")
        return metadata

    def _verified_private_rows_unlocked(
        self,
        metadata: Mapping[str, int | str],
        *,
        on_row: (
            Callable[
                [int, bytes, Message, Mapping[str, object] | None, CommittedInitial | None], None
            ]
            | None
        ) = None,
    ) -> Iterator[tuple[Message, Mapping[str, object] | None, CommittedInitial | None]]:
        """Validate the ENTIRE append-only log before any new append or trusted read.

        A later corrupt row cannot be skipped to attest an earlier row. No
        incomplete tail or quarantine is silently discarded on the private path.
        """
        from .audience_manifest import MAX_WIRE_SEQ
        from .bus_publication import _canonical
        from .coordination import canonical_publication_key

        previous_sequence = 0
        seen_keys: set[str] = set()
        if not self._path.exists():
            return
        with self._path.open("rb") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 8 * 1024 * 1024:
                    raise RelationViolationError("Oversized private bus row.")
                if not line.endswith(b"\n"):
                    raise RelationViolationError("Incomplete bus row blocks keyed publication.")
                try:
                    record = json.loads(line, object_pairs_hook=unique_wire_object)
                    if not isinstance(record, dict):
                        raise ValueError("Bus row is not an object.")
                    existing = Message.from_wire(record)
                    public = existing.to_wire()
                    raw_public = {
                        key: value for key, value in record.items() if key != PRIVATE_WIRE_FIELD
                    }
                    if (
                        type(record.get("seq")) is not int
                        or not previous_sequence < record["seq"]
                        or record["seq"] > MAX_WIRE_SEQ  # never cap by a stale marker hint
                        or any(
                            type(record.get(field)) is not str
                            for field in ("id", "from", "to", "text", "type", "sender_role")
                        )
                        or _canonical(raw_public) != _canonical(public)
                    ):
                        raise ValueError("Noncanonical public bus envelope or sequence.")
                    public_envelope_digest(public)
                    previous_sequence = existing.seq
                    if not has_private_wire_fields(record):
                        if on_row is not None:
                            on_row(offset, line, existing, None, None)
                        yield existing, None, None
                        continue
                    if set(key for key in record if key.startswith("_agent_comms_private")) != {
                        PRIVATE_WIRE_FIELD
                    }:
                        raise ValueError("Unknown private bus namespace.")
                    private = record[PRIVATE_WIRE_FIELD]
                    if (
                        not isinstance(private, dict)
                        or type(private.get("version")) is not int
                        or private["version"] != 1
                    ):
                        raise ValueError("Unsupported private bus record.")
                    if set(private) == {"version", "initial"}:
                        try:
                            initial = validate_initial_record(record, str(metadata["wire_root_id"]))
                        except (KeyError, TypeError, ValueError, OverflowError) as error:
                            raise RelationViolationError(
                                "Malformed private initial bus sideband."
                            ) from error
                        if on_row is not None:
                            on_row(offset, line, existing, None, initial)
                        yield existing, None, initial
                        continue
                    if set(private) != {"version", "response"}:
                        raise RelationViolationError(
                            "Conflicting or malformed private bus receipt."
                        )
                    receipt = private["response"]
                    if not isinstance(receipt, dict) or set(receipt) != {
                        "wire_root_id",
                        "execution_id",
                        "publication_key",
                        "envelope_digest",
                    }:
                        raise RelationViolationError(
                            "Conflicting or malformed private bus receipt."
                        )
                    if (
                        public_envelope_digest(public) != receipt["envelope_digest"]
                        or receipt["wire_root_id"] != metadata["wire_root_id"]
                        or not isinstance(receipt["execution_id"], str)
                        or not isinstance(receipt["publication_key"], str)
                        or receipt["publication_key"]
                        != canonical_publication_key(receipt["execution_id"], existing.target)
                        or receipt["publication_key"] in seen_keys
                    ):
                        raise RelationViolationError(
                            "Conflicting or malformed private bus receipt."
                        )
                    seen_keys.add(receipt["publication_key"])
                    if on_row is not None:
                        on_row(offset, line, existing, receipt, None)
                    yield existing, receipt, None
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
                    if isinstance(error, RelationViolationError):
                        raise
                    if "Duplicate bus object key" in str(error):
                        raise
                    raise RelationViolationError(
                        "Malformed public bus row blocks publication."
                    ) from error

    def _append_private_unlocked(
        self, metadata: dict[str, int | str], row: Mapping[str, object]
    ) -> None:
        """Durably reserve a sequence, append one row, then sync its parent.

        A failed append or bus parent sync leaves the outcome UNKNOWN.
        """
        encoded = json.dumps(row, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > 8 * 1024 * 1024:
            raise RelationViolationError("Private bus row exceeds the byte limit.")
        sequence_path = self._path.parent / "bus_meta.json"
        metadata["last_seq"] = row["seq"]  # type: ignore[assignment]
        _atomic_write_text(sequence_path, json.dumps(metadata, indent=2), fsync_parent=True)
        descriptor = os.open(
            self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise RelationViolationError("Private bus append target is not owner-only.")
            with os.fdopen(descriptor, "ab", closefd=False) as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        directory_fd = os.open(self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        from .private_bus_checkpoint import (
            append_private_bus_checkpoint_unlocked,
            certificate_enabled,
        )

        if certificate_enabled(self._path):
            private = row.get(PRIVATE_WIRE_FIELD)
            initial = (
                validate_initial_record(row, str(metadata["wire_root_id"]))
                if isinstance(private, dict) and "initial" in private
                else None
            )
            receipt = private.get("response") if isinstance(private, dict) else None
            public = {key: value for key, value in row.items() if key != PRIVATE_WIRE_FIELD}
            try:
                append_private_bus_checkpoint_unlocked(
                    self, metadata, encoded, Message.from_wire(public), receipt, initial
                )
            except Exception as error:
                # The bus may already contain this fsynced row. Never retry an
                # uncertain input or report publication as definitely absent.
                raise RelationViolationError(
                    "Private bus checkpoint publication outcome UNKNOWN."
                ) from error

    def publish_initial_cohort(
        self,
        message: Message,
        *,
        control: str = "ordinary",
        _bus_locked: bool = False,
        _human_origin: HumanOrigin | None = None,
    ) -> Message:
        """Commit public envelope and FULL N private decisions in the SAME fsynced row.

        This private path assumes cooperating Comms writers hold the global
        wire lock. It never publishes from a caller-supplied audience or claim.
        """
        from .audience_manifest import MAX_WIRE_SEQ, FrozenRecipient, freeze_audience
        from .wake import ControlClassification, resolve_wake_cohort

        if self._private_initial_writes is not True:
            raise RelationViolationError("Private initial publication is disabled.")
        if _human_origin is not None and type(_human_origin) is not HumanOrigin:
            raise RelationViolationError("Human initial requires a typed local USER origin.")
        if message.claim_transition is not None:
            raise RelationViolationError("An initial cohort cannot carry resource claims.")
        classification = ControlClassification(control)
        if not classification.supports_initial:
            raise RelationViolationError("System-control initial issuer is not available.")
        with nullcontext() if _bus_locked else _store_lock(self._path):
            _require_no_private_owner_rename(self._path.parent)
            metadata = self._private_marker_unlocked()
            from .private_bus_checkpoint import (
                certificate_enabled,
                verify_private_bus_checkpoint_unlocked,
            )

            if certificate_enabled(self._path):
                previous_sequence = verify_private_bus_checkpoint_unlocked(
                    self, metadata
                ).through_seq
            else:
                previous_sequence = 0
                for previous, _, _ in self._verified_private_rows_unlocked(metadata):
                    previous_sequence = previous.seq
            if int(metadata["last_seq"]) >= MAX_WIRE_SEQ:
                raise RelationViolationError("Private bus sequence is exhausted.")
            source_paths = (
                self._registry.store.path,
                self._channels.path,
                self._channels.saved_views_path,
            )
            before_revisions = tuple(file_revision(path) for path in source_paths)
            snapshot = self._registry.snapshot()
            if len({thread.created_at for thread in snapshot.threads.values()}) != len(
                snapshot.threads
            ):
                raise RelationViolationError("Registry creation identities collide.")
            sender = snapshot.aliases.get(message.sender, message.sender)
            sender_thread = snapshot.threads.get(sender)
            if sender_thread is None or not snapshot.statuses[sender].visible:
                raise RelationViolationError("Initial sender must be visible and registered.")
            if _human_origin is None:
                if not sender_thread.role.executable:
                    raise RelationViolationError(
                        "Initial sender must be a visible registered executable."
                    )
            elif (
                sender_thread.role.executable
                or message.sender != sender_thread.name
                or _human_origin.sender != sender_thread.name
                or _human_origin.created_at != sender_thread.created_at
                or _human_origin.worktree != sender_thread.worktree
            ):
                raise RelationViolationError("Local USER origin differs from registered identity.")
            if BuiltinChannel.aggregate_target(message.target) or self._channels.is_view_target(
                message.target
            ):
                raise RelationViolationError("A saved/aggregate view is not routable.")
            target = BuiltinChannel.canonical(message.target)
            tags, explicit_channels = self._channels.read()
            if not is_channel_target(target):
                if snapshot.aliases.get(target, target) != target:
                    raise RelationViolationError(
                        "Initial direct aliases need a stable send binding."
                    )
                if (
                    target not in snapshot.threads
                    or not snapshot.threads[target].role.executable
                    or not snapshot.statuses[target].visible
                ):
                    raise RelationViolationError(
                        "Initial direct target must be a visible executable."
                    )
                if target == sender:
                    raise RelationViolationError("A thread cannot message itself.")
                names = [target]
            else:
                if BuiltinChannel.lookup(target) is not None:
                    channel = Channel(target)
                else:
                    tag = target.removeprefix("#")
                    resolved_channel = explicit_channels.get(target) if tag not in tags else None
                    channel = resolved_channel or Channel(target, frozenset({tag}))
                names = [
                    name
                    for name, thread in snapshot.threads.items()
                    if name != sender
                    and thread.role.executable
                    and snapshot.statuses[name].visible
                    and channel.matches(thread.tags)
                ]
            selected = [snapshot.threads[name] for name in names]
            lookups = [stable_thread_lookup(thread.created_at) for thread in selected]
            if len(set(lookups)) != len(lookups):
                raise RelationViolationError("Recipient creation identities collide.")
            sender_lookup = stable_thread_lookup(snapshot.threads[sender].created_at)
            if sender_lookup in lookups:
                raise RelationViolationError("Sender creation identity collides with recipient.")
            stored = self._prepare_message_unlocked(
                message,
                sender=sender,
                target=target,
                sequence=max(int(metadata["last_seq"]), previous_sequence) + 1,
                snapshot=snapshot,
            )
            if _human_origin is not None:
                # The marker reserves a sequence before the row. A crash after
                # reservation can leave NO row carrying the human message ID.
                # Never admit another human input while such an UNKNOWN gap is
                # present, even if another ordinary sender subsequently skips
                # over that sequence. This intentionally favors safety over
                # availability until explicit operator reconciliation exists.
                expected_sequence = 1
                duplicate = False
                for previous, _, _ in self._verified_private_rows_unlocked(metadata):
                    if previous.seq != expected_sequence:
                        raise RelationViolationError(
                            "Private bus sequence gap has UNKNOWN outcome; "
                            "human send blocked, do not retry."
                        )
                    expected_sequence += 1
                    duplicate |= (
                        previous.sender == sender and previous.message_id == stored.message_id
                    )
                if int(metadata["last_seq"]) != expected_sequence - 1:
                    raise RelationViolationError(
                        "Private bus sequence reservation has UNKNOWN outcome; "
                        "human send blocked, do not retry."
                    )
                if duplicate:
                    raise RelationViolationError(
                        "Human initial ID already exists; inspect its receipt, do not retry."
                    )
            revision = hashlib.sha256(
                repr(
                    (
                        file_revision(self._registry.store.path),
                        file_revision(self._channels.path),
                        file_revision(self._channels.saved_views_path),
                        sorted(
                            (name, thread.created_at, sorted(thread.tags))
                            for name, thread in snapshot.threads.items()
                        ),
                        sorted(
                            (name, sorted(channel.tags))
                            for name, channel in explicit_channels.items()
                        ),
                    )
                ).encode()
            ).hexdigest()
            audience = freeze_audience(
                stored,
                tuple(
                    FrozenRecipient(lookup, name)
                    for lookup, name in zip(lookups, names, strict=True)
                ),
                revision,
                sender_lookup=sender_lookup,
                sender_name=sender,
            )
            decisions = resolve_wake_cohort(
                stored, frozen_audience=audience, control=classification
            )
            row = {
                **stored.to_wire(),
                PRIVATE_WIRE_FIELD: {
                    "version": 1,
                    "initial": initial_sideband(
                        str(metadata["wire_root_id"]),
                        stored,
                        audience,
                        decisions,
                        control=classification.value,
                    ),
                },
            }
            # Check the exact bytes and one coherent source revision before any append.
            validate_initial_record(row, str(metadata["wire_root_id"]))
            if before_revisions != tuple(file_revision(path) for path in source_paths):
                raise RelationViolationError("Send-time registry/catalog revision changed.")
            if _human_origin is None:
                self._append_private_unlocked(metadata, row)
            else:
                try:
                    self._append_private_unlocked(metadata, row)
                except BaseException as error:
                    # Even cancellation/interrupt after entry can follow a durable
                    # reservation or row. Never claim absence or retry this ID.
                    raise HumanInitialUnknownError(
                        str(metadata["wire_root_id"]), stored.seq, stored.message_id
                    ) from error
            return stored

    def read_initial_cohort(self, wire_root_id: str, wire_seq: int) -> CommittedInitial:
        """Bus-owned attestation of a committed initial row; no live re-routing."""
        from .audience_manifest import MAX_WIRE_SEQ

        if type(wire_seq) is not int or not 0 < wire_seq <= MAX_WIRE_SEQ:
            raise ValueError("wire_seq must be a positive SQLite-range integer.")
        with _store_lock(self._path):
            metadata = self._private_marker_unlocked()
            if wire_root_id != metadata["wire_root_id"]:
                raise RelationViolationError("Initial wire root does not match the bus marker.")
            matched: CommittedInitial | None = None
            for _message, _receipt, initial in self._verified_private_rows_unlocked(metadata):
                if initial is not None and initial.message.seq == wire_seq:
                    matched = initial
            if matched is None:
                raise RelationViolationError(
                    "No committed initial sideband for this wire sequence."
                )
            return matched

    def _keyed_receipt_unlocked(
        self, intent: PublicationIntent
    ) -> tuple[Message | None, int, dict[str, int | str]]:
        """Read the whole owner-only bus before trusting an exact keyed receipt.

        Caller holds the bus file lock. Absence is NOT authorization to append.
        """
        from .coordination import PublicationIntent

        if type(intent) is not PublicationIntent:
            raise TypeError("Keyed response requires a validated PublicationIntent.")
        metadata = self._private_marker_unlocked()
        matched: Message | None = None
        previous_sequence = 0
        for existing, receipt, _initial in self._verified_private_rows_unlocked(metadata):
            previous_sequence = existing.seq
            if receipt is not None and receipt["publication_key"] == intent.publication_key:
                if receipt["execution_id"] != intent.execution_id:
                    raise RelationViolationError("Response publication identity conflicts.")
                matched = existing
        if matched is not None:
            expected = intent.expected_message
            if (
                matched.sender != intent.sender
                or matched.target != intent.exact_target
                or matched.body != expected.body
                or matched.type is not expected.type
                or matched.timestamp != expected.timestamp
                or matched.notice != expected.notice
                or matched.membership != expected.membership
                or matched.message_id != intent.expected_message_id
            ):
                raise RelationViolationError("Response publication intent conflicts.")
        return matched, previous_sequence, metadata

    def read_keyed_response(self, intent: PublicationIntent) -> Message | None:
        """Read-only exact receipt resolution; never append or repair an absent row."""
        with _store_lock(self._path):
            matched, _, _ = self._keyed_receipt_unlocked(intent)
            return matched

    def publish_keyed_response(self, intent: PublicationIntent) -> Message:
        """Default-OFF fsynced append; runtime owner fencing needs a coordinator."""
        if self._private_response_writes is not True:
            raise RelationViolationError("Private response publication is disabled.")
        with _store_lock(self._path):
            return self._publish_keyed_response_unlocked(intent)

    def _publish_keyed_response_unlocked(
        self, intent: PublicationIntent, *, registry_snapshot: RegistrySnapshot | None = None
    ) -> Message:
        """Internal append with bus lock; a supplied registry snapshot stays locked."""
        from .audience_manifest import MAX_WIRE_SEQ
        from .coordination import canonical_publication_key

        if self._private_response_writes is not True:
            raise RelationViolationError("Private response publication is disabled.")
        matched, previous_sequence, metadata = self._keyed_receipt_unlocked(intent)
        if matched is not None:
            return matched
        message = intent.expected_message
        # A durable exact replay above wins even after registry/route renames.
        sender, target = self._validate_publish_request(
            message, registry_snapshot=registry_snapshot
        )
        if registry_snapshot is None:
            executable = self._registry.require(sender).role.executable
        else:
            executable = registry_snapshot.threads[sender].role.executable
        if not executable:
            raise RelationViolationError("Keyed response sender must be executable.")
        canonical_target = BuiltinChannel.canonical(target)
        if intent.publication_key != canonical_publication_key(
            intent.execution_id, canonical_target
        ):
            raise RelationViolationError("Response publication key does not match its route.")
        last_sequence = max(int(metadata["last_seq"]), previous_sequence)
        if last_sequence >= MAX_WIRE_SEQ:
            raise RelationViolationError("Private bus sequence is exhausted.")
        stored = self._prepare_message_unlocked(
            message,
            sender=sender,
            target=target,
            sequence=last_sequence + 1,
            snapshot=registry_snapshot,
        )
        if stored.message_id != intent.expected_message_id:
            raise RelationViolationError("Stored response does not match expected Message ID.")
        public = stored.to_wire()
        row = {
            **public,
            PRIVATE_WIRE_FIELD: {
                "version": 1,
                "response": {
                    "wire_root_id": metadata["wire_root_id"],
                    "execution_id": intent.execution_id,
                    "publication_key": intent.publication_key,
                    "envelope_digest": public_envelope_digest(public),
                },
            },
        }
        self._append_private_unlocked(metadata, row)
        return stored

    def _next_seq(self) -> int:
        return self.latest_sequence() + 1

    @staticmethod
    def _marker_key(name: str, target: str) -> str:
        return json.dumps([name, target], separators=(",", ":"))

    def inbox(self, name: str, target: str | None = None) -> Sequence[Message]:
        delivery = self._delivery_scope(name)
        name = delivery.actor
        matches = self._scope_filter(delivery, target)
        if self.reads.human(self._registry.require(name).role):
            seen = self.reads.seen_sequences(name, self._registry.snapshot())
            return [
                message
                for message in self._load_log()
                if delivery.delivers(message.sender, message.target)
                and matches(message)
                and message.seq not in seen
            ]
        markers = self._read_markers()
        global_read = markers.get(name, 0)
        with _store_lock(self._path):
            return [
                msg
                for msg in self._iter_log_unlocked()
                if msg.seq > global_read
                and delivery.delivers(msg.sender, msg.target)
                and matches(msg)
                and msg.seq
                > max(
                    global_read,
                    markers.get(
                        self._marker_key(name, delivery.conversation(msg.sender, msg.target)), 0
                    ),
                )
            ]

    def pending_count(self, name: str, target: str | None = None) -> int:
        """Count unread messages without retaining their bodies."""
        counts = self.pending_counts(name)
        if target is None or BuiltinChannel.aggregate_target(target):
            return sum(counts.values())
        if is_channel_target(target) or BuiltinChannel.is_alias(target):
            targets = self._channels.history_targets(target)
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
            targets = self._channels.history_targets(target)
            return lambda message: targets is None or message.target in targets
        peer = self._registry.require(target).name
        return lambda message: delivery.conversation(message.sender, message.target) == peer

    def pending_counts(self, name: str) -> Mapping[str, int]:
        """Count one thread's unread messages by conversation in one log pass."""
        if self.reads.human(self._registry.require(name).role):
            delivery = self._delivery_scope(name)
            revision = tuple(
                file_revision(path)
                for path in (
                    self._path,
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
            with _store_lock(self._path):
                try:
                    with BusRouteCounts(self._path) as route_counts:
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
                for message in self._iter_log_unlocked():
                    if (
                        delivery.delivers(message.sender, message.target)
                        and message.seq not in seen
                    ):
                        conversation = delivery.conversation(message.sender, message.target)
                        counts[conversation] = counts.get(conversation, 0) + 1
            self._pending_cache[name] = PendingCounts(revision, delivery, counts)
            return dict(counts)
        revision = tuple(
            file_revision(path)
            for path in (
                self._path,
                self._channels.path,
                self.reads.path.with_name(self.reads.legacy_filename),
            )
        )
        delivery = self._delivery_scope(name)
        cached = self._pending_cache.get(name)
        if cached is not None and cached.revision == revision and cached.delivery == delivery:
            return dict(cached.counts)
        markers = self._read_markers()
        global_read = markers.get(delivery.actor, 0)
        counts: dict[str, int] = {}
        with _store_lock(self._path):
            try:
                with BusRouteCounts(self._path) as route_counts:
                    if route_counts.sync(self._pending_route_fields):
                        for target, raw_sender in route_counts.routes():
                            if not delivery.delivers(raw_sender, target):
                                continue
                            scope = delivery.conversation(raw_sender, target)
                            cutoff = max(
                                global_read,
                                markers.get(self._marker_key(delivery.actor, scope), 0),
                            )
                            unread = route_counts.pair_after(target, raw_sender, cutoff)
                            if unread:
                                counts[scope] = counts.get(scope, 0) + unread
                        self._pending_cache[name] = PendingCounts(revision, delivery, counts)
                        return dict(counts)
            except (OSError, sqlite3.DatabaseError):
                # The JSONL bus remains authoritative if its disposable index fails.
                pass
            for message in self._iter_log_unlocked():
                if message.seq <= global_read or not delivery.delivers(
                    message.sender, message.target
                ):
                    continue
                scope = delivery.conversation(message.sender, message.target)
                if message.seq <= max(
                    global_read,
                    markers.get(self._marker_key(delivery.actor, scope), 0),
                ):
                    continue
                counts[scope] = counts.get(scope, 0) + 1
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
        if target.startswith("#") and target != GLOBAL_CHANNEL:
            tag = target[1:]
            if not tag or not set(tag) <= _TAG_CHARS:
                raise ValueError(f"Channel {target!r} has an invalid tag.")
        return seq, sender, target

    def _iter_pending_routes_unlocked(self) -> Iterator[tuple[int, str, str]]:
        for record, _ in _iter_jsonl_records(self._path):
            yield self._pending_route_fields(record)

    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        """Count selected inboxes in one locked wire pass for a thread listing.

        The CLI starts a new process for each call, so the per-viewer cache in
        ``pending_counts`` cannot amortize one scan per registered thread.
        Build a single registry/channel delivery snapshot, then decode each
        wire row only once. This is a read projection, never a read ACK.
        """
        snapshot = self._registry.snapshot()
        humans = tuple(
            name
            for name in names
            if snapshot.aliases.get(name, name) in snapshot.threads
            and self.reads.human(snapshot.threads[snapshot.aliases.get(name, name)].role)
        )
        if humans:
            counts = {name: sum(self.pending_counts(name).values()) for name in humans}
            executors = tuple(name for name in names if name not in humans)
            if executors:
                counts.update(self.pending_counts_all(executors))
            return counts
        actors: dict[str, str] = {}
        deliveries: dict[str, DeliveryScope] = {}
        for name in names:
            actor = snapshot.aliases.get(name, name)
            if actor not in snapshot.threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            actors[name] = actor
            if actor not in deliveries:
                thread = snapshot.threads[actor]
                deliveries[actor] = DeliveryScope(
                    thread.name, snapshot.aliases, self._channels.targets_for(thread.tags)
                )
        if not deliveries:
            return {}
        markers = self._read_markers()
        # A scoped marker key is JSON-encoded. Decoding it once matters as much
        # as the one-pass wire scan: rebuilding it for every recipient of every
        # broadcast would recreate a threads × messages serialization loop.
        scoped_markers: dict[str, dict[str, int]] = {actor: {} for actor in deliveries}
        for key, sequence in markers.items():
            if not isinstance(key, str) or not key.startswith("["):
                continue
            try:
                scope = json.loads(key)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(scope, list)
                and len(scope) == 2
                and isinstance(scope[0], str)
                and isinstance(scope[1], str)
                and scope[0] in scoped_markers
                and key == self._marker_key(scope[0], scope[1])
            ):
                scoped_markers[scope[0]][scope[1]] = sequence
        # Every channel message has the same conversation scope for its
        # recipients. Sort their read thresholds once and range-add each row's
        # eligible recipients: O(messages log threads), not O(messages × threads).
        channel_members: dict[str, list[str]] = {}
        channel_cutoffs: dict[str, list[int]] = {}
        channel_deltas: dict[str, list[int]] = {}
        channel_self_cutoffs: dict[str, dict[str, int]] = {}
        channel_direct_actor: dict[str, str] = {}
        channels: dict[str, list[DeliveryScope]] = {}
        for delivery in deliveries.values():
            for target in delivery.channels:
                channels.setdefault(target, []).append(delivery)
        for target, recipients in channels.items():
            # Legacy "broadcast" is a channel route, but if a real thread has
            # that name its conversation scope is the sender, not "broadcast".
            direct_actor = (
                snapshot.aliases.get(target, target) if not target.startswith("#") else ""
            )
            if direct_actor in deliveries:
                channel_direct_actor[target] = direct_actor
            ordinary = [
                (
                    max(
                        markers.get(delivery.actor, 0),
                        scoped_markers[delivery.actor].get(target, 0),
                    ),
                    delivery.actor,
                )
                for delivery in recipients
                if delivery.actor != direct_actor
            ]
            ordinary.sort()
            channel_cutoffs[target] = [after for after, _ in ordinary]
            channel_members[target] = [actor for _, actor in ordinary]
            channel_self_cutoffs[target] = {actor: after for after, actor in ordinary}
            channel_deltas[target] = [0] * (len(ordinary) + 1)
        counts = dict.fromkeys(deliveries, 0)
        with _store_lock(self._path):
            try:
                with BusRouteCounts(self._path) as route_counts:
                    if route_counts.sync(self._pending_route_fields):
                        senders: dict[str, list[str]] = {}
                        for target, raw_sender in route_counts.routes():
                            senders.setdefault(target, []).append(raw_sender)
                            sender = snapshot.aliases.get(raw_sender, raw_sender)
                            route_actor: str | None
                            if target in channel_cutoffs:
                                route_actor = channel_direct_actor.get(target)
                            else:
                                route_actor = snapshot.aliases.get(target, target)
                            if route_actor in counts and route_actor != sender:
                                cutoff = max(
                                    markers.get(route_actor, 0),
                                    scoped_markers[route_actor].get(sender, 0),
                                )
                                counts[route_actor] += route_counts.pair_after(
                                    target, raw_sender, cutoff
                                )
                        for target, members in channel_members.items():
                            for actor in members:
                                cutoff = channel_self_cutoffs[target][actor]
                                total = route_counts.target_after(target, cutoff)
                                for raw_sender in senders.get(target, ()):
                                    if snapshot.aliases.get(raw_sender, raw_sender) == actor:
                                        total -= route_counts.pair_after(target, raw_sender, cutoff)
                                counts[actor] += total
                        return {name: counts[actor] for name, actor in actors.items()}
            except (OSError, sqlite3.DatabaseError):
                # The bus remains authoritative if its disposable index is
                # unavailable. Route validation errors still fail closed.
                pass
            for seq, raw_sender, target in self._iter_pending_routes_unlocked():
                sender = snapshot.aliases.get(raw_sender, raw_sender)
                if target in channel_cutoffs:
                    eligible = bisect_left(channel_cutoffs[target], seq)
                    if eligible:
                        deltas = channel_deltas[target]
                        deltas[0] += 1
                        deltas[eligible] -= 1
                        # Sending to one's own channel never creates unread.
                        own_after = channel_self_cutoffs[target].get(sender)
                        if own_after is not None and seq > own_after:
                            counts[sender] -= 1
                    direct = channel_direct_actor.get(target)
                    if (
                        direct is not None
                        and direct != sender
                        and seq > max(markers.get(direct, 0), scoped_markers[direct].get(sender, 0))
                    ):
                        counts[direct] += 1
                else:
                    actor = snapshot.aliases.get(target, target)
                    if (
                        actor in deliveries
                        and actor != sender
                        and seq > max(markers.get(actor, 0), scoped_markers[actor].get(sender, 0))
                    ):
                        counts[actor] += 1
        for target, members in channel_members.items():
            running = 0
            for actor, delta in zip(members, channel_deltas[target], strict=False):
                running += delta
                counts[actor] += running
        return {name: counts[actor] for name, actor in actors.items()}

    def mark_delivered(self, name: str, target: str | None = None) -> int:
        """Mark unread messages delivered and return the count without retaining them."""
        if self.reads.human(self._registry.require(name).role):
            messages = self.inbox(name, target)
            basis = self.reads.capture(name, messages, self._registry.snapshot(), self._path)
            self.reads.mark_displayed(basis.viewer, basis)
            return len(messages)
        delivery = self._delivery_scope(name)
        name = delivery.actor
        matches = self._scope_filter(delivery, target)
        markers = self._read_markers()
        global_read = markers.get(name, 0)
        count = 0
        latest = 0
        scoped: dict[str, int] = {}
        with _store_lock(self._path):
            for msg in self._iter_log_unlocked():
                if (
                    msg.seq > global_read
                    and delivery.delivers(msg.sender, msg.target)
                    and matches(msg)
                    and msg.seq
                    > max(
                        global_read,
                        markers.get(
                            self._marker_key(name, delivery.conversation(msg.sender, msg.target)), 0
                        ),
                    )
                ):
                    count += 1
                    latest = msg.seq
                    scoped[
                        self._marker_key(name, delivery.conversation(msg.sender, msg.target))
                    ] = msg.seq
        if not count:
            return 0
        self._write_markers({name: latest} if target is None else scoped)
        return count

    def mark_delivered_through(self, name: str, sequence: int) -> None:
        """Advance a thread's global inbox cursor without loading messages."""
        canonical = self._registry.require(name).name
        if sequence < 0:
            raise ValueError("Delivery sequence cannot be negative.")
        if self.reads.human(self._registry.require(canonical).role):
            messages = (message for message in self.inbox(canonical) if message.seq <= sequence)
            displayed = self.reads.capture(
                canonical, messages, self._registry.snapshot(), self._path
            )
            self.reads.mark_displayed(canonical, displayed)
            return
        self._write_markers({canonical: sequence})

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, in seq order."""
        a = self._registry.require(a).name
        b = self._registry.require(b).name
        return [
            msg
            for msg in self._load_log()
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
        targets = self._channels.history_targets(target)
        return [msg for msg in self._load_log() if targets is None or msg.target in targets]

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
        a_names = self._registry.aliases_for(a)
        b_names = self._registry.aliases_for(b)
        return self._history_page(
            lambda message: (
                (message.sender in a_names and message.target in b_names)
                or (message.sender in b_names and message.target in a_names)
            ),
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
            targets=a_names | b_names,
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
        targets = self._channels.history_targets(target)
        return self._history_page(
            lambda message: targets is None or message.target in targets,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
            targets=targets,
        )

    def channel_display_page(
        self,
        scope: ChannelDisplayScope,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Browse a captured presentation scope, not a delivery/history authority."""
        with self._record_snapshot() as (_, records):
            return self._collect_history_page(
                records,
                scope.includes,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
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
        with _store_lock(self._path):
            try:
                with BusPageIndex(self._path) as index:
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
                    self._public_page_record(record, size)
                    for record, size in _iter_jsonl_records(self._path)
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
        with self._path.open("rb") as stream:

            def has_match(*, lower: int | None, upper: int | None) -> bool:
                with closing(
                    index.offsets(lower=lower, upper=upper, descending=True, targets=targets)
                ) as rows:
                    for row in rows:
                        message, _ = self._public_page_record(*index.record(stream, row))
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
                    message, encoded_size = self._public_page_record(*index.record(stream, row))
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
    def _public_page_record(record: Mapping, raw_size: int) -> tuple[Message, int]:
        """Charge public page budgets for public bytes, never private sidebands."""
        message = Message.from_wire(record)
        if has_private_wire_fields(record):
            return message, len(json.dumps(message.to_wire()).encode()) + 1
        return message, raw_size

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

    def full_history(self) -> Sequence[Message]:
        """Every message on the wire, in seq order (the combined view)."""
        return self._load_log()

    @contextmanager
    def _record_snapshot(
        self, *, need_sequence: bool = True
    ) -> Iterator[tuple[int, Iterator[tuple[Message, int]]]]:
        """Fixed opened-inode/byte boundary with public page-size accounting.

        Display-only callers do not use the sequence watermark. Skipping its
        missing-metadata fallback avoids a full log scan while their short
        cross-store wire lock is held; ``through=0`` then means unrequested.
        Export/history retain the default authoritative watermark behavior.
        """
        sequence_path = self._path.parent / "bus_meta.json"
        with _store_lock(self._path):
            if need_sequence and _claim_gate_enabled(self._path):
                # Metadata reserves a sequence BEFORE the append. A failed
                # append must never surface as a committed message watermark.
                through = self._max_sequence_unlocked()
            else:
                through = self._read_last_sequence(sequence_path) if need_sequence else 0
                if need_sequence and not through and self._path.exists():
                    through = self._max_sequence_unlocked()
            try:
                stream: BinaryIO | None = self._path.open("rb")
            except FileNotFoundError:
                stream = None
            boundary = stream.seek(0, 2) if stream is not None else 0
            if stream is not None:
                stream.seek(0)
        try:
            records = (
                (
                    self._public_page_record(record, size)
                    for record, size in _iter_jsonl_stream(
                        stream, boundary=boundary, label="wire snapshot"
                    )
                )
                if stream is not None
                else iter(())
            )
            yield through, records
        finally:
            if stream is not None:
                stream.close()

    @contextmanager
    def full_history_snapshot(self) -> Iterator[tuple[int, Iterator[Message]]]:
        """Open one fixed append boundary without retaining the complete wire."""
        with self._record_snapshot() as (through, records):
            yield through, (message for message, _ in records)

    def last_sent_timestamps(self) -> Mapping[str, float]:
        """Aggregate sent times without retaining message bodies."""
        with _store_lock(self._path):
            projection = BusActivityIndex(self._path).snapshot(
                file_revision(self._path), self._bus_activity_fields
            )
            if projection is not None:
                return projection[1]
            latest: dict[str, float] = {}
            for message in self._iter_log_unlocked():
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
        return self._history_page(
            lambda message: True, before=before, after=after, limit=limit, max_bytes=max_bytes
        )

    @staticmethod
    @lru_cache(maxsize=4)
    def _receipt_offsets(
        path: Path, revision: tuple[int, int, int, int] | None
    ) -> Mapping[str, int]:
        offsets: dict[str, int] = {}
        if revision is None:
            return offsets
        with path.open("rb") as stream:
            while True:
                offset = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    if not raw.endswith(b"\n"):
                        break
                    raise
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    offsets[record["id"]] = offset
        return offsets

    def message_by_id(self, message_id: str) -> Message | None:
        """Look up a durable receipt without retaining the wire's message bodies."""
        with _store_lock(self._path):
            offset = self._receipt_offsets(self._path, file_revision(self._path)).get(message_id)
            if offset is None:
                return None
            with self._path.open("rb") as stream:
                stream.seek(offset)
                return Message.from_wire(json.loads(stream.readline()))

    def channels(self) -> Sequence[str]:
        return list(self._channels.views())

    def total_messages(self) -> int:
        with _store_lock(self._path):
            return sum(1 for _ in _iter_jsonl_records(self._path))

    def latest_sequence(self) -> int:
        """Return the global high-water sequence without loading message bodies."""
        sequence_path = self._path.parent / "bus_meta.json"
        with _store_lock(self._path):
            if _claim_gate_enabled(self._path):
                return self._max_sequence_unlocked()
            sequence = self._read_last_sequence(sequence_path)
            return sequence if sequence else self._max_sequence_unlocked()

    def _read_markers(self) -> dict[str, int]:
        marker_path = self.reads.path.with_name(self.reads.legacy_filename)
        with _store_lock(marker_path):
            return self._read_markers_unlocked(marker_path)

    @staticmethod
    def _read_markers_unlocked(marker_path: Path) -> dict[str, int]:
        if not marker_path.exists():
            return {}
        markers: dict[str, int] = json.loads(marker_path.read_text())
        return markers

    def _write_markers(self, markers: dict[str, int]) -> None:
        marker_path = self.reads.path.with_name(self.reads.legacy_filename)
        with _store_lock(marker_path):
            current = self._read_markers_unlocked(marker_path)
            for name, sequence in markers.items():
                current[name] = max(sequence, current.get(name, 0))
            _atomic_write_text(marker_path, json.dumps(current, indent=2))

    def _load_log(self) -> list[Message]:
        with _store_lock(self._path):
            return self._load_log_unlocked()

    def _load_log_unlocked(self) -> list[Message]:
        return list(self._iter_log_unlocked())

    def _iter_log_unlocked(self) -> Iterator[Message]:
        for record, _ in _iter_jsonl_records(self._path):
            yield Message.from_wire(record)

    def _max_sequence_unlocked(self) -> int:
        return max(
            (int(record.get("seq", 0)) for record, _ in _iter_jsonl_records(self._path)),
            default=0,
        )

    def _last_row_sequence_unlocked(self) -> int:
        """Read the final complete row without rescanning the whole bus on send."""
        try:
            with self._path.open("rb") as records:
                records.seek(0, os.SEEK_END)
                end = records.tell()

                def previous_newline(before: int) -> int:
                    cursor = before
                    while cursor:
                        start = max(0, cursor - 64 * 1024)
                        records.seek(start)
                        offset = records.read(cursor - start).rfind(b"\n")
                        if offset >= 0:
                            return start + offset
                        cursor = start
                    return -1

                last_newline = previous_newline(end)
                if last_newline < 0:
                    return 0  # An incomplete first row is repaired before append.
                prior_newline = previous_newline(last_newline)
                records.seek(prior_newline + 1)
                raw = records.read(last_newline - prior_newline - 1)
        except FileNotFoundError:
            return 0
        try:
            row = json.loads(raw, object_pairs_hook=unique_wire_object)
            if type(row) is not dict or type(row.get("seq")) is not int or row["seq"] < 1:
                raise ValueError("Invalid last bus sequence")
            return int(row["seq"])
        except (ValueError, UnicodeError) as error:
            raise RelationViolationError("Malformed last bus row blocks publication.") from error

    def rename_thread(self, old_name: str, new_name: str) -> None:
        """Move read markers to canonical names without rewriting message history."""
        marker_path = self.reads.path.with_name(self.reads.legacy_filename)
        with _store_lock(marker_path):
            markers = self._read_markers_unlocked(marker_path)
            renamed: dict[str, int] = {}
            for key, sequence in markers.items():
                if key == old_name:
                    key = new_name
                else:
                    try:
                        scope = json.loads(key)
                    except json.JSONDecodeError:
                        scope = None
                    if isinstance(scope, list) and len(scope) == 2:
                        scope = [new_name if value == old_name else value for value in scope]
                        key = json.dumps(scope, separators=(",", ":"))
                renamed[key] = max(sequence, renamed.get(key, 0))
            _atomic_write_text(marker_path, json.dumps(renamed, indent=2))

    @staticmethod
    def _read_last_sequence(sequence_path: Path) -> int:
        if not sequence_path.exists():
            return 0
        data = json.loads(sequence_path.read_text())
        return int(data.get("last_seq", 0))

    def assert_legacy_rewrite_allowed(self) -> None:
        """Reject a deletion before registry state changes if private proof may exist."""
        with _store_lock(self._path):
            self._assert_no_private_authority_unlocked()

    def _assert_no_private_authority_unlocked(self) -> None:
        sequence_path = self._path.parent / "bus_meta.json"
        if sequence_path.exists():
            metadata = json.loads(sequence_path.read_text())
            if not isinstance(metadata, Mapping):
                raise RelationViolationError("Bus sequence metadata is not an object.")
            if "writer_protocol_version" in metadata:
                raise RelationViolationError("Private bus protocol blocks legacy deletion.")
        if not self._path.exists():
            return
        with self._path.open("rb") as records:
            if records.seek(0, os.SEEK_END):
                records.seek(-1, os.SEEK_END)
                if records.read(1) != b"\n":
                    raise RelationViolationError("Incomplete bus row blocks legacy deletion.")
        for record, _ in _iter_jsonl_records(self._path):
            if has_private_wire_fields(record):
                raise RelationViolationError("Private bus authority blocks legacy deletion.")

    def remove_thread(self, name: str) -> tuple[int, int]:
        """Purge legacy messages, never rewriting a private authority row."""
        names = self._registry.aliases_for(name)
        with _store_lock(self._path):
            self._assert_no_private_authority_unlocked()
            messages = self._load_log_unlocked()
            retained = [
                message
                for message in messages
                if message.sender not in names and message.target not in names
            ]
            sequence_path = self._path.parent / "bus_meta.json"
            high_water = max(
                self._read_last_sequence(sequence_path),
                max((message.seq for message in messages), default=0),
            )
            _atomic_write_text(
                sequence_path, json.dumps({"last_seq": high_water}, indent=2), fsync_parent=True
            )
            _atomic_write_text(
                self._path,
                "".join(f"{json.dumps(message.to_wire())}\n" for message in retained),
            )

        marker_path = self.reads.path.with_name(self.reads.legacy_filename)
        with _store_lock(marker_path):
            markers = self._read_markers_unlocked(marker_path)

            def references_thread(key: str) -> bool:
                if key in names:
                    return True
                try:
                    scope = json.loads(key)
                except json.JSONDecodeError:
                    return False
                return (
                    isinstance(scope, list) and len(scope) == 2 and bool(names.intersection(scope))
                )

            retained_markers = {
                key: sequence for key, sequence in markers.items() if not references_thread(key)
            }
            _atomic_write_text(marker_path, json.dumps(retained_markers, indent=2))
        return len(messages) - len(retained), len(markers) - len(retained_markers)
