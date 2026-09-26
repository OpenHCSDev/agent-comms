"""OpenHCS agent communications — operations layer.

Business logic on top of the declaration-owned core. Every UI adapter
(Toad via ACP, VS Code, pi extension, CLI) calls these operations; none of
them own orchestration semantics.

Fail-closed throughout: every operation proves required relations before
acting, and raises on unregistered references.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .channels import ChannelCatalog
from .goal_history import GoalHistoryEntry
from .goal_mentions import bind_goal_mentions
from .goal_pauses import GoalPauseEvent, GoalPauseEvents
from .goal_waits import GoalInputReview, GoalWait, GoalWaits

if TYPE_CHECKING:
    from .goal_attempts import GoalAttemptStore
    from .relationships import ThreadRelationships
from .declarations import (
    Activity,
    ActivityLog,
    ActivityState,
    AgentRuntimeInfo,
    BuiltinChannel,
    Channel,
    ChannelActivity,
    ChannelDisplayScope,
    ChannelSort,
    ChannelView,
    CoordinationSnapshot,
    DMDisplayBasis,
    FinishedTurnFence,
    Goal,
    GoalExecution,
    GoalPauseSource,
    GoalWaitTarget,
    MembershipChange,
    Message,
    MessageBus,
    MessagePage,
    MessageRoute,
    MessageType,
    RegistrySnapshot,
    RelationViolationError,
    RuntimeInfoStore,
    SavedView,
    SharedLedger,
    Tag,
    Thread,
    ThreadRegistry,
    ThreadRole,
    ThreadSort,
    ThreadStatus,
    ThreadView,
    TurnClaimFence,
    TurnRouting,
    UnregisteredThreadError,
    WireRevision,
    _atomic_write_text,
    _store_lock,
    current_thread,
    file_revision,
    is_channel_target,
)
from .envelope_claim_transitions import ClaimProjection
from .exporting import (
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportReceipt,
    WireExportScope,
    WireExportScopeKind,
    WireTranscriptExporter,
)
from .importing import ImportFormat, ImportLimits, ImportReceipt
from .tool_results import ToolDiff
from .transcript_routes import InputDisplay, TranscriptRoutes

OBSERVATION_INTERVAL = 0.05


def _required_block_reason(reason: str | None) -> str:
    """Validate a new block's own reason; prior progress is never a fallback."""
    if type(reason) is not str or not reason.strip():
        raise ValueError("Blocking a goal requires a nonempty reason for the needed input.")
    normalized = reason.strip()
    if len(normalized) > 1024:
        raise ValueError("A blocked-goal reason must be at most 1024 characters.")
    return normalized


def _owner_launch_proof(name: str, pid: int, epoch: int) -> bytes:
    """Fixed-size pipe proof bound to the complete name and owner incarnation.

    A valid thread name has no protocol length limit. Passing its plaintext
    under the wire lock could fill the pipe before the child can acquire it.
    """
    identity = json.dumps((name, pid, epoch), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).digest()


def _session_model(session_file: Path) -> tuple[str, str] | None:
    """Read the last model choice from a pi session file.

    Non-interactive pi requires an explicit provider/model, so forks replay the
    parent's final ``model_change`` entry. Returns ``(provider, model_id)`` or
    ``None`` when the session carries no model record.
    """
    try:
        lines = _reverse_lines(session_file)
    except OSError:
        return None
    for line in lines:
        if b'"model_change"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "model_change":
            provider = entry.get("provider")
            model_id = entry.get("modelId")
            if provider and model_id:
                return str(provider), str(model_id)
    return None


def _reverse_lines(path: Path, *, max_bytes: int | None = None) -> Iterator[bytes]:
    """Read JSONL newest-first without allocating the file or oversized lines."""
    try:
        with path.open("rb") as stream:
            position = stream.seek(0, 2)
            floor = max(0, position - max_bytes) if max_bytes is not None else 0
            pending = b""
            oversized = False
            while position > floor:
                count = min(65536, position - floor)
                position -= count
                stream.seek(position)
                parts = (stream.read(count) + pending).split(b"\n")
                pending = parts.pop(0)
                for line in reversed(parts):
                    if oversized:
                        oversized = False
                        continue
                    if line:
                        yield line
                if len(pending) > 256 * 1024:
                    pending = b""
                    oversized = True
            if floor == 0 and pending and not oversized:
                yield pending
    except OSError:
        return


@dataclass(frozen=True, slots=True)
class ForkSpec:
    """Declares one fork: a child thread spawned from a parent's session."""

    name: str
    parent: str
    task: str
    tags: frozenset[str] = frozenset()
    prompt: str | None = None

    def __post_init__(self) -> None:
        if not self.task:
            raise ValueError("Fork task cannot be empty.")


@dataclass(frozen=True, slots=True)
class DeleteThreadResult:
    name: str
    messages_removed: int
    markers_removed: int
    activity_events_removed: int
    runtime_removed: bool
    ledger_references_removed: int
    detached_children: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RenameThreadResult:
    previous: str
    current: str
    changed: bool


@dataclass(frozen=True, slots=True)
class ProjectChangeResult:
    thread: str
    previous: str
    current: str
    changed: bool


@dataclass(frozen=True, slots=True)
class OwnerRestartResult:
    thread: str
    previous_pid: int
    pid: int


@dataclass(frozen=True, slots=True)
class OwnerStartResult:
    thread: str
    pid: int
    launched: bool


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """One normalized event from a thread's persisted Pi transcript."""

    kind: str
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    raw_input: object | None = None
    ok: bool = True
    routing: TurnRouting | None = None
    diff: ToolDiff | None = None

    @classmethod
    def from_wire(cls, data: Mapping) -> TranscriptEvent:
        return cls(
            **{
                **data,
                "routing": TurnRouting.from_wire(data["routing"]) if data.get("routing") else None,
                "diff": ToolDiff(**data["diff"]) if data.get("diff") else None,
            }
        )

    def to_wire(self, *, include_diff: bool = True) -> dict[str, object]:
        data = {**asdict(self), "routing": self.routing.to_wire() if self.routing else None}
        if self.diff is None or not include_diff:
            data.pop("diff", None)
        return data


@dataclass(frozen=True, slots=True)
class TranscriptCursor:
    session_file: str
    offset: int


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    events: tuple[TranscriptEvent, ...]
    before: TranscriptCursor
    after: TranscriptCursor
    has_older: bool
    has_newer: bool

    def metadata(self) -> dict[str, object]:
        return {
            item.name: asdict(value) if isinstance(value, TranscriptCursor) else value
            for item in fields(self)
            if item.name != "events"
            for value in (getattr(self, item.name),)
        }


def _reverse_records(path: Path, before: int) -> Generator[tuple[int, int, bytes], None, None]:
    """Seek backwards in chunks; never parse or allocate the preceding history."""
    with path.open("rb") as stream:
        position = before
        end = before
        pending = b""
        while position:
            count = min(position, 65536)
            position -= count
            stream.seek(position)
            pending = stream.read(count) + pending
            while (boundary := pending.rfind(b"\n", 0, len(pending) - 1)) >= 0:
                start = position + boundary + 1
                yield start, end, pending[boundary + 1 :]
                pending, end = pending[: boundary + 1], start
        if pending:
            yield 0, end, pending


class TagAction(Enum):
    LIST = "list"
    CREATE = "create"
    RENAME = "rename"
    DELETE = "delete"

    def apply(self, comms: Comms, name: str = "", new_name: str = "") -> frozenset[str]:
        match self:
            case self.CREATE:
                comms.create_tag(name)
            case self.RENAME:
                comms.rename_tag(name, new_name)
            case self.DELETE:
                comms.delete_tag(name)
        return comms.channel_catalog.tags()


class Comms:
    """Wire of registry, bus, and ledger rooted at one directory."""

    @cached_property
    def relationships(self) -> ThreadRelationships:
        """Explicit work declarations and read-only thread relationship views."""
        from .relationships import ThreadRelationships

        return ThreadRelationships(self)

    def __init__(
        self,
        root: Path,
        *,
        private_initial_writes: bool = True,
        private_claim_writes: bool = True,
    ) -> None:
        from .view_unread import transcript_read_state

        self.root = Path(root).expanduser()
        if private_initial_writes or private_claim_writes:
            # Create a private fresh root before the registry lock can create it
            # using the caller's ordinary umask. Existing roots are never chmod-repaired.
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.registry = ThreadRegistry(self.root / "registry.json")
        self.transcript_routes = TranscriptRoutes(self.root / "transcript_routes.json")
        self.channel_catalog = ChannelCatalog(self.root / "channels.json", self.registry)
        self.bus = MessageBus(
            self.root / "bus.jsonl",
            self.registry,
            private_initial_writes=private_initial_writes,
            private_claim_writes=private_claim_writes,
        )
        self.ledger = SharedLedger(self.root / "ledger.json")
        self.activity = ActivityLog(self.root / "activity.jsonl")
        self.runtime_info = RuntimeInfoStore(self.root / "runtime_info.json")
        self.transcript_reads = transcript_read_state(self.root / "thread_read_markers.json")
        self._wire_lock_path = self.root / "wire"
        self._sent_times_signature: tuple[int, int, int] | None = None
        self._sent_times: dict[str, float] = {}

    # ─── Messaging ────────────────────────────────────────────────────────────

    def send(
        self,
        sender: str,
        target: str,
        body: str,
        type: MessageType = MessageType.INFO,
        *,
        notice: bool = False,
        claims: Sequence[str | Path] = (),
        releases: Sequence[str | Path] = (),
    ) -> str:
        """Declare a message and route it through the bus."""
        return self.send_message(
            sender, target, body, type, notice=notice, claims=claims, releases=releases
        ).message_id

    def send_message(
        self,
        sender: str,
        target: str,
        body: str,
        type: MessageType = MessageType.INFO,
        *,
        notice: bool = False,
        claims: Sequence[str | Path] = (),
        releases: Sequence[str | Path] = (),
    ) -> Message:
        """Return one committed envelope, including optional guarded claims."""
        with _store_lock(self._wire_lock_path):
            if sender not in self.registry:
                raise UnregisteredThreadError(f"Sender {sender!r} is not registered.")
            owner = self.registry.require(sender)
            if not owner.role.executable:
                raise RelationViolationError(
                    "Human messages require the explicit user-message operation."
                )
            message = Message(sender=owner.name, target=target, body=body, type=type, notice=notice)
            if claims or releases:
                # Legacy registries may predate the new-thread uniqueness check.
                # A shared creation identity must never become claim release
                # authority for two otherwise unrelated registered owners.
                incarnations = [
                    thread.created_at for thread in self.registry.all_threads().values()
                ]
                if len(set(incarnations)) != len(incarnations):
                    raise RelationViolationError("Registry creation identities collide.")
                return self.bus.publish_claim_envelope(
                    message,
                    worktree=Path(owner.worktree),
                    incarnation=str(owner.created_at),
                    claims=claims,
                    releases=releases,
                )
            return self.bus.publish(message)

    def initialize_private_initial_protocol(self) -> str:
        """Initialize the private protocol on a fresh owner-only root."""
        with _store_lock(self._wire_lock_path):
            return self.bus.initialize_private_protocol()

    def initialize_private_claim_protocol(self) -> str:
        """Initialize the claim read barrier on a fresh marked private bus."""
        with _store_lock(self._wire_lock_path):
            return self.bus.initialize_private_claim_protocol()

    def claim_projection(self) -> ClaimProjection:
        """Read current whole-set ownership from one verified bus authority."""
        return self.bus.claim_projection()

    def send_initial_cohort(
        self,
        sender: str,
        target: str,
        body: str,
        type: MessageType = MessageType.INFO,
        *,
        notice: bool = False,
    ) -> Message:
        """Test-gated full-N send. No automatic SQL acceptance or live wake."""
        with _store_lock(self._wire_lock_path):
            if sender not in self.registry or not self.registry.require(sender).role.executable:
                raise RelationViolationError("Initial sender must be a registered executable.")
            return self.bus.publish_initial_cohort(
                Message(sender=sender, target=target, body=body, type=type, notice=notice)
            )

    def sent_tool_message(self, name: str, output: str, ok: bool) -> Message | None:
        """Resolve a successful send receipt, including older ID-only tool results."""
        if name != "comms_send" or not ok:
            return None
        try:
            receipt = json.loads(output)
            if not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str):
                return None
            if isinstance(receipt.get("message"), dict):
                message = Message.from_wire(receipt["message"])
                return message if message.message_id == receipt["id"] else None
            return self.bus.message_by_id(receipt["id"])
        except (ValueError, KeyError, TypeError):
            return None

    def broadcast(self, sender: str, body: str) -> str:
        """Declare a message addressed to every peer."""
        return self.send(sender, "broadcast", body)

    def user_identity(self, worktree: str) -> Thread:
        """One durable human sender, never an executor or a tag-derived agent role."""
        with _store_lock(self._wire_lock_path):
            for thread in self.registry.all_threads().values():
                if not thread.role.executable:
                    return thread
            name, suffix = "user", 2
            while self.registry.name_reserved(name):
                name, suffix = f"user-{suffix}", suffix + 1
            thread = Thread(name, frozenset(), worktree, role=ThreadRole.USER)
            self.registry.register(thread)
            return thread

    def send_user_message(self, target: str, body: str, *, worktree: str) -> Message:
        user = self.user_identity(worktree)
        with _store_lock(self._wire_lock_path):
            return self.bus.publish(Message(user.name, target, body, MessageType.INFO))

    def inbox(self, name: str, target: str | None = None) -> Sequence[Message]:
        """Undelivered messages for one thread, optionally scoped to a conversation."""
        return self.bus.inbox(name, target)

    def acknowledge(self, name: str, target: str | None = None) -> int:
        """Mark an inbox or one conversation delivered. Returns count acknowledged."""
        with _store_lock(self._wire_lock_path):
            return self.bus.mark_delivered(name, target)

    def incoming_page(self, name: str, *, after: int, limit: int = 100) -> MessagePage:
        return self.bus.incoming_page(name, after=after, limit=limit)

    def acknowledge_through(self, name: str, sequence: int) -> None:
        with _store_lock(self._wire_lock_path):
            self.bus.mark_delivered_through(self.registry.require(name).name, sequence)

    def pending_count(self, name: str, target: str | None = None) -> int:
        return self.bus.pending_count(name, target)

    def pending_counts(self, name: str) -> Mapping[str, int]:
        """Unread counts by channel or DM peer, computed in one log pass."""
        return self.bus.pending_counts(name)

    # ─── IRC views ────────────────────────────────────────────────────────────

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, in seq order."""
        return self.bus.dm_history(a, b)

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full history of one channel (``#all`` or a tag channel)."""
        return self.bus.channel_history(target)

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
        """Bounded DM history for any client adapter."""
        return self.bus.dm_history_page(
            a, b, before=before, after=after, limit=limit, max_bytes=max_bytes
        )

    def dm_display_page(
        self,
        peer: str,
        *,
        worktree: str,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Fetch a bounded human DM page and its immutable identity/read basis.

        Fetching is not paint proof. A UI may use the basis only after proving
        that the corresponding inbound tail was contiguous and visibly painted.
        """
        if not isinstance(peer, str) or is_channel_target(peer) or peer == "broadcast":
            raise ValueError("A DM page requires a registered peer.")
        viewer = self.user_identity(worktree).name
        marker_path = self.bus._path.parent / "read_markers.json"
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            revision = file_revision(self.registry._path)
            viewer_name = snapshot.aliases.get(viewer, viewer)
            peer_name = snapshot.aliases.get(peer, peer)
            viewer_thread = snapshot.threads.get(viewer_name)
            peer_thread = snapshot.threads.get(peer_name)
            if (
                viewer_thread is None
                or viewer_thread.role is not ThreadRole.USER
                or peer_thread is None
                or viewer_name == peer_name
            ):
                raise ValueError("DM display identity changed; refresh the page.")
            viewer_names = frozenset(
                {
                    viewer_name,
                    *(name for name, owner in snapshot.aliases.items() if owner == viewer_name),
                }
            )
            peer_names = frozenset(
                {
                    peer_name,
                    *(name for name, owner in snapshot.aliases.items() if owner == peer_name),
                }
            )
            marker_revision = file_revision(marker_path)
            page = self.bus.dm_history_page(
                viewer_name,
                peer_name,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )
            older_unread = False
            if page.has_older and page.oldest_seq is not None:
                markers = self.bus._read_markers()
                baseline = max(
                    markers.get(viewer_name, 0),
                    markers.get(self.bus._marker_key(viewer_name, peer_name), 0),
                )
                with self.bus._record_snapshot(need_sequence=False) as (_, records):
                    older_unread = any(
                        message.seq < page.oldest_seq
                        and message.seq > baseline
                        and message.sender in peer_names
                        and message.target in viewer_names
                        for message, _ in records
                    )
            if (
                file_revision(self.registry._path) != revision
                or file_revision(marker_path) != marker_revision
            ):
                raise ValueError("DM display changed while paging; refresh the page.")
            root_info = self.root.stat()
            try:
                bus_info = self.bus._path.stat()
            except FileNotFoundError:
                bus_identity = None
            else:
                bus_identity = (bus_info.st_dev, bus_info.st_ino)
            return replace(
                page,
                display_basis=DMDisplayBasis(
                    root=str(self.root.resolve()),
                    root_identity=(root_info.st_dev, root_info.st_ino),
                    worktree=str(Path(worktree).resolve()),
                    requested_peer=peer,
                    viewer=viewer_name,
                    viewer_epoch=snapshot.owner_epochs[viewer_name],
                    viewer_created_at=viewer_thread.created_at,
                    viewer_names=viewer_names,
                    peer=peer_name,
                    peer_epoch=snapshot.owner_epochs[peer_name],
                    peer_created_at=peer_thread.created_at,
                    peer_names=peer_names,
                    registry_revision=revision,
                    marker_revision=marker_revision,
                    bus_identity=bus_identity,
                    newest_seq=page.newest_seq,
                    older_unread=older_unread,
                ),
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
        """Bounded channel history for any client adapter."""
        return self.bus.channel_history_page(
            target, before=before, after=after, limit=limit, max_bytes=max_bytes
        )

    def _display_basis_revision(self) -> tuple:
        """Revisions of every store used to define one display predicate and marker."""
        return tuple(
            file_revision(path)
            for path in (
                self.registry._path,
                self.channel_catalog.path,
                self.channel_catalog.metadata_path,
                self.channel_catalog.pins_path,
                self.channel_catalog.saved_views_path,
                self.bus._path.parent / "read_markers.json",
            )
        )

    def _capture_display_basis(self, viewer: str | None, revision: tuple) -> tuple:
        """Capture one basis while the caller holds the short wire lock."""
        registry = self.registry.snapshot()
        channels = self.channel_catalog.views(registry.threads)
        order = self.channel_catalog.list_order
        pins = self.channel_catalog.pinned_threads_snapshot()
        markers = self.bus._read_markers() if viewer is not None else {}
        canonical_viewer = registry.aliases.get(viewer, viewer) if viewer is not None else None
        viewer_names = (
            frozenset(
                {
                    canonical_viewer,
                    *(
                        name
                        for name, owner in registry.aliases.items()
                        if owner == canonical_viewer
                    ),
                }
            )
            if canonical_viewer is not None
            else frozenset()
        )
        scopes: list[ChannelDisplayScope] = []
        reset_channels: list[str] = []
        for channel in channels.values():
            members = frozenset(
                name
                for name, thread in registry.threads.items()
                if channel.any_mode and channel.exact and channel.tags <= thread.tags
            )
            participant_names = members | frozenset(
                name for name, owner in registry.aliases.items() if owner in members
            )
            targets = (
                channel.builtin.history_targets
                if channel.builtin is not None
                else frozenset({channel.name})
            )
            expanded_after = 0
            if canonical_viewer is not None and channel.exact and channel.builtin is None:
                exact_key = self.bus._view_marker_key(canonical_viewer, channel.name, "exact")
                after = markers.get(exact_key, 0)
                if channel.any_mode:
                    participant_basis = self.bus.any_participant_basis(channel, registry)
                    expanded_after = markers.get(
                        self.bus._view_marker_key(
                            canonical_viewer, channel.name, "any", participant_basis
                        ),
                        0,
                    )
                if (
                    self.bus._marker_key(canonical_viewer, channel.name) in markers
                    and exact_key not in markers
                ):
                    reset_channels.append(channel.name)
            else:
                after = (
                    max(
                        markers.get(canonical_viewer, 0),
                        markers.get(self.bus._marker_key(canonical_viewer, channel.name), 0),
                    )
                    if canonical_viewer is not None
                    else 0
                )
            scopes.append(
                ChannelDisplayScope(
                    channel.name,
                    targets,
                    channel.any_mode,
                    participant_names,
                    after,
                    revision,
                    expanded_after,
                )
            )
        notice = (
            "Read positions were reset for "
            + ", ".join(sorted(reset_channels))
            + "; reopen the channel to review its messages."
            if reset_channels
            else None
        )
        return (
            registry,
            channels,
            tuple(scopes),
            order,
            canonical_viewer,
            viewer_names,
            pins,
            notice,
        )

    @contextmanager
    def _display_snapshot(
        self, viewer: str | None = None, target: str | None = None
    ) -> Iterator[tuple]:
        """Open one bus boundary atomically with a validated display basis.

        Direct registry/catalog writers need not hold the Comms wire lock; the
        revision checks detect their changes before the opened bus boundary.
        The raw scan happens after releasing that lock. A later append is not
        retried: the opened inode and byte limit already define a valid point.
        """
        for _ in range(3):
            with ExitStack() as stack:
                with _store_lock(self._wire_lock_path):
                    revision = self._display_basis_revision()
                    basis = self._capture_display_basis(viewer, revision)
                    if self._display_basis_revision() != revision:
                        continue
                    bus_revision = file_revision(self.bus._path)
                    _, records = stack.enter_context(self.bus._record_snapshot(need_sequence=False))
                    if self._display_basis_revision() != revision:
                        continue
                    if file_revision(self.bus._path) != bus_revision:
                        bus_revision = None
                if target is not None and target not in basis[1]:
                    # A newly-created channel must not be rejected from an
                    # earlier basis without checking its current revision.
                    if self._display_basis_revision() != revision:
                        continue
                    raise ValueError(f"Unknown channel: {target!r}")
                yield basis, records, bus_revision
                return
        raise RuntimeError("Display scope changed during snapshot; retry the request.")

    def channel_display_page(
        self,
        target: str,
        *,
        worktree: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Local display projection; underlying channel history remains target-owned."""
        if not is_channel_target(target):
            raise ValueError(f"{target!r} is not a channel target.")
        viewer = self.user_identity(worktree).name if worktree is not None else None
        with self._display_snapshot(viewer=viewer, target=target) as (basis, records, _):
            scope = next(item for item in basis[2] if item.channel == target)
            page = self.bus._collect_history_page(
                records,
                scope.includes,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )
            return replace(page, display_scope=scope)

    def message_high_water(self) -> int:
        """Global message cursor used by polling clients to avoid idle scans."""
        return self.bus.latest_sequence()

    def last_sent_timestamps(self) -> Mapping[str, float]:
        """Latest explicit outgoing message per sender, cached until the log changes."""
        path = self.root / "bus.jsonl"
        try:
            stat = path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return {}
        if signature != self._sent_times_signature:
            self._sent_times = dict(self.bus.last_sent_timestamps())
            self._sent_times_signature = signature
        canonical: dict[str, float] = {}
        for sender, timestamp in self._sent_times.items():
            name = self.registry.canonical_name(sender)
            canonical[name] = max(canonical.get(name, 0.0), timestamp)
        return canonical

    def full_history(self) -> Sequence[Message]:
        """Every message on the wire, in seq order (the combined view)."""
        return self.bus.full_history()

    def full_history_page(
        self,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        return self.bus.full_history_page(
            before=before, after=after, limit=limit, max_bytes=max_bytes
        )

    def export_wire(
        self,
        destination: Path | str,
        *,
        format: WireExportFormat,
        scope: WireExportScope,
        limit: WireExportLimit,
        overwrite: bool = False,
        export_started_at: float | None = None,
    ) -> WireExportReceipt:
        """Export one fixed, non-mutating snapshot of authoritative wire rows."""
        started_at = time.time() if export_started_at is None else export_started_at
        with ExitStack() as stack:
            with _store_lock(self._wire_lock_path):
                matches: Callable[[Message], bool]
                if scope.kind is WireExportScopeKind.EVERYTHING:
                    canonical_scope = scope

                    def matches_everything(_message: Message) -> bool:
                        return True

                    matches = matches_everything
                elif scope.kind is WireExportScopeKind.CHANNEL:
                    assert scope.channel is not None
                    if self.channel_catalog.is_view_target(scope.channel):
                        raise RelationViolationError(
                            f"Saved view {scope.channel!r} has no authoritative wire history."
                        )
                    targets = self.channel_catalog.history_targets(scope.channel)
                    if targets is None:
                        raise RelationViolationError(
                            f"View {scope.channel!r} is an aggregate, not an "
                            "exportable conversation."
                        )
                    canonical_scope = scope

                    def matches_channel(message: Message) -> bool:
                        return message.target in targets

                    matches = matches_channel
                else:
                    first = self.registry.require(scope.participants[0]).name
                    second = self.registry.require(scope.participants[1]).name
                    canonical_scope = WireExportScope.for_dm(first, second)
                    first_names = self.registry.aliases_for(first)
                    second_names = self.registry.aliases_for(second)

                    def matches_dm(message: Message) -> bool:
                        return (
                            message.sender in first_names and message.target in second_names
                        ) or (message.sender in second_names and message.target in first_names)

                    matches = matches_dm
                through, messages = stack.enter_context(self.bus.full_history_snapshot())

            selected = (message for message in messages if matches(message))
            return WireTranscriptExporter(
                format=format,
                scope=canonical_scope,
                limit=limit,
                boundary=WireExportBoundary(through, started_at),
            ).export(selected, Path(destination).expanduser(), overwrite=overwrite)

    # ─── Activity (live feedback) ─────────────────────────────────────────────

    def set_activity(self, thread: str, state: ActivityState, detail: str = "") -> None:
        """Declare a thread's current activity (thinking/working/idle)."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(thread).name
            self.activity.emit(Activity(thread=canonical, state=state, detail=detail))

    def activity_of(self, thread: str) -> Activity:
        participant = self.registry.require(thread)
        return self.activity.current(participant.name, active=participant.executing)

    def all_activity(self) -> Mapping[str, Activity]:
        active = frozenset(t.name for t in self.registry.all_threads().values() if t.executing)
        return self.activity.all_current(active=active)

    def begin_turn(
        self, name: str, turn_id: str, detail: str = "", routing: TurnRouting | None = None
    ) -> TurnClaimFence:
        with _store_lock(self._wire_lock_path):
            claimed, _ = self.registry.claim_local_turn(name, turn_id, routing=routing)
            try:
                self.activity.emit(Activity(claimed.name, ActivityState.THINKING, detail))
            except BaseException:
                self.registry.finish_claimed_turn(claimed.name, turn_id)
                raise
            assert claimed.active_turn is not None
            admission = claimed.active_turn.admission_generation
            assert type(admission) is int
            return TurnClaimFence(
                claimed.name, claimed.created_at, turn_id, claimed.turn_generation, admission
            )

    def finish_turn(
        self, name: str, turn_id: str, *, expected: TurnClaimFence | None = None
    ) -> FinishedTurnFence | None:
        """Persist exact terminal identity; ID-only legacy release cannot attest a fence."""
        with _store_lock(self._wire_lock_path):
            released, fence = self.registry.finish_claimed_turn_with_fence(
                name, turn_id, expected=expected
            )
            if not released:
                return None
            self.activity.emit(Activity(self.registry.canonical_name(name), ActivityState.IDLE))
            return fence

    def set_agent_info(
        self,
        thread: str,
        *,
        model: str | None = None,
        session_name: str | None = None,
        context_used: int | None = None,
        context_size: int | None = None,
    ) -> None:
        """Record the latest model and context metadata for a thread."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(thread).name
            self.runtime_info.set(
                AgentRuntimeInfo(
                    thread=canonical,
                    model=model,
                    session_name=session_name,
                    context_used=context_used,
                    context_size=context_size,
                )
            )

    def agent_info_of(self, thread: str) -> AgentRuntimeInfo | None:
        return self.runtime_info.get(self.registry.require(thread).name)

    def all_agent_info(self) -> Mapping[str, AgentRuntimeInfo]:
        return self.runtime_info.all()

    def channels(self) -> Sequence[str]:
        return list(self.channel_catalog.views())

    def channel_views(
        self, *, show_stopped: bool = True, show_archived: bool = False
    ) -> tuple[ChannelView, ...]:
        """Channel declarations and current members; clients never infer membership."""
        snapshot = self.registry.snapshot()
        channels = self.channel_catalog.views(snapshot.threads)
        return self._channel_views_for(
            snapshot,
            channels,
            self.channel_catalog.pinned_threads_snapshot(),
            self.channel_catalog.list_order,
            show_stopped=show_stopped,
            show_archived=show_archived,
        )

    def _channel_views_for(
        self,
        snapshot: RegistrySnapshot,
        channels: Mapping[str, Channel],
        pins: Mapping[str, frozenset[str]],
        order: ChannelSort,
        *,
        show_stopped: bool,
        show_archived: bool,
        display_activity: Mapping[str, ChannelActivity] | None = None,
    ) -> tuple[ChannelView, ...]:
        people = {
            name: thread
            for name, thread in snapshot.threads.items()
            if snapshot.statuses[name].in_view(
                show_stopped=show_stopped, show_archived=show_archived
            )
            and thread.role.executable
        }
        activity = self.all_activity()
        sent = self.last_sent_timestamps()
        messages = self.bus.channel_activity() if display_activity is None else display_activity
        views: list[ChannelView] = []
        for channel in channels.values():
            pinned_members = pins.get(channel.name, frozenset())
            members = tuple(
                sorted(
                    (name for name, thread in people.items() if channel.matches(thread.tags)),
                    key=lambda name: (
                        name not in pinned_members,
                        channel.order.key(
                            name,
                            people[name].created_at,
                            activity[name].timestamp if name in activity else 0,
                            sent.get(name, 0),
                        ),
                    ),
                )
            )
            if display_activity is None:
                targets = (
                    channel.builtin.history_targets
                    if channel.builtin is not None
                    else frozenset({channel.name})
                )
                history = (
                    tuple(messages.values())
                    if targets is None
                    else tuple(messages.get(target, ChannelActivity()) for target in targets)
                )
            else:
                history = (messages.get(channel.name, ChannelActivity()),)
            views.append(
                ChannelView(
                    channel,
                    members,
                    max(
                        max((item.last_message for item in history), default=0),
                        max(
                            (activity[name].timestamp for name in members if name in activity),
                            default=0,
                        ),
                    ),
                    max((item.last_user_input for item in history), default=0),
                    pinned_members & frozenset(members),
                )
            )
        return tuple(sorted(views, key=order.key))

    def thread_views(
        self, *, show_stopped: bool = True, show_archived: bool = False
    ) -> tuple[ThreadView, ...]:
        return self._thread_views_for(
            self.registry.snapshot(), show_stopped=show_stopped, show_archived=show_archived
        )

    def _thread_views_for(
        self, snapshot: RegistrySnapshot, *, show_stopped: bool, show_archived: bool
    ) -> tuple[ThreadView, ...]:
        runtime = self.runtime_info.all()
        active = frozenset(t.name for t in snapshot.threads.values() if t.executing)
        activities = self.activity.all_current(active=active)
        waits = GoalWaits(self.root / "goal_waits.json").snapshot()
        return tuple(
            ThreadView(
                thread,
                snapshot.statuses[name],
                activities.get(name, Activity(name, ActivityState.IDLE, timestamp=0)),
                runtime.get(name),
                snapshot.last_seen.get(name, 0),
                GoalWaits.execution(thread.goal, waits, snapshot),
            )
            for name, thread in snapshot.threads.items()
            if snapshot.statuses[name].in_view(
                show_stopped=show_stopped, show_archived=show_archived
            )
            and thread.role.executable
        )

    def coordination_snapshot(
        self, actor: str = "", *, show_stopped: bool = True, show_archived: bool = False
    ) -> CoordinationSnapshot:
        channels = self.channel_views(show_stopped=show_stopped, show_archived=show_archived)
        unread = self.pending_counts(actor) if actor in self.registry else {}
        channel_unread: dict[str, int] = {}
        for view in channels:
            targets = self.channel_catalog.history_targets(view.channel.name)
            channel_unread[view.channel.name] = sum(
                count for target, count in unread.items() if targets is None or target in targets
            )
        return CoordinationSnapshot(
            self.thread_views(show_stopped=show_stopped, show_archived=show_archived),
            channels,
            unread,
            self.last_sent_timestamps(),
            channel_unread,
            self.channel_catalog.list_order,
            show_stopped=show_stopped,
            show_archived=show_archived,
        )

    def viewer_snapshot(
        self, worktree: str, *, show_stopped: bool = True, show_archived: bool = False
    ) -> CoordinationSnapshot:
        """Local presentation scope, independent of agent delivery cursors."""
        viewer = self.user_identity(worktree).name
        with self._display_snapshot(viewer=viewer) as (basis, records, bus_revision):
            registry, declarations, scopes, order, captured_viewer, viewer_names, pins, notice = (
                basis
            )
            assert captured_viewer is not None
            activity_scopes = tuple(
                replace(scope, after=0, basis_revision=scope.basis_revision[:-1])
                for scope in scopes
            )
            display_activity, display_unread = self.bus.display_view_metrics(
                records, scopes, activity_scopes, captured_viewer, viewer_names, bus_revision
            )
            channels = self._channel_views_for(
                registry,
                declarations,
                pins,
                order,
                show_stopped=show_stopped,
                show_archived=show_archived,
                display_activity=display_activity,
            )
            threads = self._thread_views_for(
                registry, show_stopped=show_stopped, show_archived=show_archived
            )
            return CoordinationSnapshot(
                threads,
                channels,
                self.pending_counts(captured_viewer),
                self.last_sent_timestamps(),
                display_unread,
                order,
                self.transcript_reads.counts(
                    captured_viewer,
                    {view.thread.name: view.thread.session_file or "" for view in threads},
                    self._is_unread_reply,
                ),
                show_stopped=show_stopped,
                show_archived=show_archived,
                read_marker_notice=notice,
            )

    def _is_unread_reply(self, record: Mapping) -> bool:
        message = record.get("message")
        return (
            isinstance(message, Mapping)
            and message.get("role") == "assistant"
            and any(
                event.kind in {"assistant", "notice"} and event.text.strip()
                for event in self._transcript_record_events(record)
            )
        )

    def mark_thread_view_read(self, name: str, *, worktree: str, through: TranscriptCursor) -> None:
        """Acknowledge only the native transcript boundary actually displayed."""
        viewer = self.user_identity(worktree).name
        thread = self.registry.require(name)
        if through.session_file != thread.session_file:
            raise ValueError("Transcript changed; refresh before marking it read.")
        self.transcript_reads.mark_read(viewer, through.session_file, through.offset)

    def mark_channel_view_read(
        self,
        target: str,
        *,
        worktree: str,
        through: int | None = None,
        expected_scope: ChannelDisplayScope | None = None,
    ) -> None:
        viewer = self.user_identity(worktree).name
        with _store_lock(self._wire_lock_path):
            captured_keys = None
            if through is not None:
                if expected_scope is None or expected_scope.channel != target:
                    raise ValueError("Channel display scope missing; refresh the displayed page.")
                revision = self._display_basis_revision()
                basis = self._capture_display_basis(viewer, revision)
                current = next((scope for scope in basis[2] if scope.channel == target), None)
                if current != expected_scope or self._display_basis_revision() != revision:
                    raise ValueError("Channel display changed; refresh the displayed page.")
                channel = basis[1][target]
                if channel.exact and channel.builtin is None:
                    keys = [self.bus._view_marker_key(viewer, target, "exact")]
                    if channel.any_mode:
                        participant_basis = self.bus.any_participant_basis(channel, basis[0])
                        keys.append(
                            self.bus._view_marker_key(viewer, target, "any", participant_basis)
                        )
                    captured_keys = tuple(keys)
                else:
                    captured_keys = (self.bus._marker_key(viewer, target),)
            self.bus.mark_view_read(
                viewer,
                target,
                self.bus.latest_sequence() if through is None else through,
                captured_keys=captured_keys,
            )

    def mark_dm_view_read(
        self,
        peer: str,
        *,
        worktree: str,
        through: int,
        expected_display_basis: DMDisplayBasis,
    ) -> None:
        """CAS one painted human DM tail, never a global/executor ACK.

        The page must have no omitted unread inbound messages. The caller must
        additionally prove its through-bound was actually and contiguously
        painted; a fetched page alone cannot establish visibility.
        """
        proof = expected_display_basis
        if type(proof) is not DMDisplayBasis or type(through) is not int:
            raise ValueError("Painted DM read requires a typed page basis and integer bound.")
        if (
            proof.root != str(self.root.resolve())
            or proof.worktree != str(Path(worktree).resolve())
            or proof.requested_peer != peer
            or proof.newest_seq is None
            or proof.older_unread
            or not 0 <= through <= proof.newest_seq
        ):
            raise ValueError("Painted DM read does not match a contiguous displayed page.")
        with _store_lock(self._wire_lock_path), _store_lock(self.registry._path):
            root_info = self.root.stat()
            if (root_info.st_dev, root_info.st_ino) != proof.root_identity:
                raise ValueError("DM root was replaced; refresh the page.")
            if file_revision(self.registry._path) != proof.registry_revision:
                raise ValueError("DM registry changed; refresh the page.")
            snapshot = self.registry._snapshot_unlocked()
            # Match user_identity(worktree)'s current first-human selection
            # without reentering the registry lock or creating a new user.
            selected_viewer = next(
                (thread for thread in snapshot.threads.values() if not thread.role.executable),
                None,
            )
            viewer = snapshot.threads.get(proof.viewer)
            target = snapshot.threads.get(proof.peer)
            if (
                selected_viewer is None
                or selected_viewer.name != proof.viewer
                or viewer is None
                or viewer.role is not ThreadRole.USER
                or target is None
                or snapshot.aliases.get(peer, peer) != proof.peer
                or viewer.created_at != proof.viewer_created_at
                or target.created_at != proof.peer_created_at
                or snapshot.owner_epochs.get(proof.viewer) != proof.viewer_epoch
                or snapshot.owner_epochs.get(proof.peer) != proof.peer_epoch
                or frozenset(
                    {
                        proof.viewer,
                        *(
                            name
                            for name, owner in snapshot.aliases.items()
                            if owner == proof.viewer
                        ),
                    }
                )
                != proof.viewer_names
                or frozenset(
                    {
                        proof.peer,
                        *(name for name, owner in snapshot.aliases.items() if owner == proof.peer),
                    }
                )
                != proof.peer_names
            ):
                raise ValueError("DM viewer/peer incarnation changed; refresh the page.")
            try:
                bus_info = self.bus._path.stat()
            except FileNotFoundError:
                bus_identity = None
            else:
                bus_identity = (bus_info.st_dev, bus_info.st_ino)
            if bus_identity != proof.bus_identity:
                raise ValueError("DM bus was replaced; refresh the page.")
            self.bus._mark_dm_painted_bound(
                proof.viewer,
                proof.peer,
                through,
                expected_marker_revision=proof.marker_revision,
            )

    def mark_user_view_read(self, target: str, *, worktree: str) -> None:
        """Explicit human 'Mark inbox read' for a channel, DM, or native thread.

        Unlike the agent-facing acknowledgement tool, this never advances an
        executor's delivery cursor. A deliberate mark-read can acknowledge the
        current saved tail without pretending the transcript was displayed.
        """
        if is_channel_target(target):
            self.mark_channel_view_read(target, worktree=worktree)
            return
        viewer = self.user_identity(worktree).name
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(target)
            checkpoint = self.transcript_checkpoint(thread.name)
            if checkpoint.session_file and Path(checkpoint.session_file).is_file():
                self.transcript_reads.mark_read(viewer, checkpoint.session_file, checkpoint.offset)
            self.bus.mark_delivered(viewer, thread.name)

    def revision(self) -> WireRevision:
        """A cheap observer token; activity expiry is checked without rescanning idle logs."""
        return WireRevision(
            tuple(
                file_revision(path)
                for path in (
                    self.registry._path,
                    self.channel_catalog.path,
                    self.channel_catalog.pins_path,
                    self.channel_catalog.metadata_path,
                    self.channel_catalog.saved_views_path,
                    self.bus._path,
                    self.activity._path,
                    self.runtime_info._path,
                    self.root / "read_markers.json",
                    self.root / "goal_waits.json",
                    self.transcript_reads.path,
                )
            ),
            int(time.time()),
        )

    def _require_available_new_tags(self, proposed: frozenset[str]) -> None:
        """Validate every tag identity before any enclosing state mutation."""
        known = self.channel_catalog.tags()
        for tag in proposed - known:
            self.channel_catalog.require_available_tag_name(tag)

    def create_tag(self, name: str) -> Tag:
        tag = Tag(name)
        with _store_lock(self._wire_lock_path):
            self.channel_catalog.require_available_name(tag.name)
            if tag.name in self.channel_catalog.tags():
                return tag
            self.channel_catalog.require_available_tag_name(tag.name)
            tags, channels = self.channel_catalog.read()
            self.channel_catalog.write(tags | {tag.name}, channels)
        return tag

    def _rebase_passive_channel_scope(self, name: str) -> None:
        """Membership changes cut off former scope without claiming input delivery."""
        from .passive_channel_awareness import PassiveChannelAwareness

        awareness = PassiveChannelAwareness(self.root)
        # Even checking for an optional ledger can fail after the membership
        # commit. A failed probe skips the advisory; owner reads stay strict.
        try:
            if not awareness.path.exists():
                return
        except (OSError, TypeError, ValueError):
            return
        owner = self.registry.require(name)
        snapshot = self.registry.snapshot()
        # The membership write already committed. Advisory storage is
        # optional; a stale scope row suppresses its next-turn frame.
        with suppress(OSError, TypeError, ValueError):
            awareness.scope_changed(
                owner,
                admission=snapshot.admission_generations[owner.name],
                high_water=self.message_high_water(),
                channels=self.channel_catalog.targets_for(owner.tags),
            )

    def update_tags(
        self, name: str, *, add: frozenset[str] = frozenset(), remove: frozenset[str] = frozenset()
    ) -> Thread:
        for tag in add | remove:
            Tag(tag)
        with _store_lock(self._wire_lock_path):
            self._require_available_new_tags(add)
            thread = self.registry.require(name)
            previous_channels = self.channel_catalog.views()
            updated = replace(thread, tags=(thread.tags | add) - remove)
            self.registry.register(updated, self.registry.status(thread.name))
            updated = self.registry.require(thread.name)
            self.channel_catalog.remember_tags(add, time.time())
            if thread.role.executable and thread.tags != updated.tags:
                channels = {**previous_channels, **self.channel_catalog.views()}
                for channel in channels.values():
                    before, after = channel.matches(thread.tags), channel.matches(updated.tags)
                    if before != after:
                        change = MembershipChange.JOINED if after else MembershipChange.LEFT
                        self.bus.publish(
                            Message(
                                thread.name,
                                channel.name,
                                f"{thread.name} {change.value} {channel.name}",
                                MessageType.INFO,
                                membership=change,
                            )
                        )
            if thread.tags != updated.tags:
                self._rebase_passive_channel_scope(updated.name)
            return updated

    def set_channel(self, name: str, tags: frozenset[str]) -> Channel:
        with _store_lock(self._wire_lock_path):
            self.channel_catalog.require_available_name(name)
            previous = self.channel_catalog.resolve(name if name.startswith("#") else f"#{name}")
            channel = Channel(name, tags, previous.order)
            if channel.builtin is not None:
                raise ValueError("Built-in channels cannot be changed.")
            name_tag = channel.name.removeprefix("#")
            if name_tag in self.channel_catalog.tags() and channel.tags != frozenset({name_tag}):
                raise ValueError(
                    "A named compatibility audience cannot replace an exact tag channel."
                )
            self._require_available_new_tags(channel.tags)
            known, channels = self.channel_catalog.read()
            channels[channel.name] = channel
            self.channel_catalog.write(known | channel.tags, channels)
            return self.channel_catalog.resolve(channel.name)

    def saved_views(self) -> Mapping[str, SavedView]:
        return self.channel_catalog.saved_views()

    def set_saved_view(self, view: SavedView) -> SavedView:
        with _store_lock(self._wire_lock_path):
            return self.channel_catalog.set_view(view)

    def delete_saved_view(self, name: str) -> None:
        with _store_lock(self._wire_lock_path):
            self.channel_catalog.delete_view(name)

    def set_channel_metadata(self, name: str, *, parent: str | None, archived: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            return self.channel_catalog.set_metadata(name, parent=parent, archived=archived)

    def set_channel_any_mode(self, name: str, enabled: bool) -> Channel:
        """Local UI preference, not same-user authentication or a routing decision."""
        with _store_lock(self._wire_lock_path):
            return self.channel_catalog.set_any_mode(name, enabled)

    def set_channel_sort(self, name: str, order: ThreadSort) -> Channel:
        with _store_lock(self._wire_lock_path):
            return self.channel_catalog.set_order(name, order)

    def set_channel_order(self, order: ChannelSort) -> ChannelSort:
        """Persist channel-list ordering independently of each channel's members."""
        with _store_lock(self._wire_lock_path):
            return self.channel_catalog.set_list_order(order)

    def set_channel_pinned(self, name: str, pinned: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            return self.channel_catalog.set_pinned(name, pinned)

    def set_thread_pinned(self, channel: str, name: str, pinned: bool) -> None:
        canonical_channel = channel if channel.startswith("#") else f"#{channel}"
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            view = self.channel_catalog.views().get(canonical_channel)
            if view is None:
                raise ValueError(f"Unknown channel: {canonical_channel!r}")
            if pinned and (
                not view.matches(thread.tags)
                or not thread.role.executable
                or not self.registry.status(thread.name).visible
            ):
                raise ValueError(f"Thread {thread.name!r} is not a member of {canonical_channel}.")
            self.channel_catalog.set_thread_pinned(canonical_channel, thread.name, pinned)

    def delete_channel(self, name: str) -> None:
        canonical = name if name.startswith("#") else f"#{name}"
        if BuiltinChannel.lookup(canonical):
            raise ValueError("Built-in channels cannot be deleted.")
        with _store_lock(self._wire_lock_path):
            tags, channels = self.channel_catalog.read()
            if canonical not in channels:
                raise ValueError("This is an automatic tag view; manage its tag instead.")
            del channels[canonical]
            self.channel_catalog.remove_channel(
                canonical, remove_metadata=canonical.removeprefix("#") not in tags
            )
            self.channel_catalog.write(tags, channels)

    def rename_tag(self, name: str, new_name: str) -> None:
        Tag(new_name)
        if name == new_name:
            if name not in self.channel_catalog.tags():
                raise ValueError(f"Unknown tag: {name!r}")
            return
        self._change_tag(name, new_name)

    def delete_tag(self, name: str) -> None:
        self._change_tag(name, None)

    def _change_tag(self, name: str, replacement: str | None) -> None:
        Tag(name)
        with _store_lock(self._wire_lock_path):
            if name not in self.channel_catalog.tags():
                raise ValueError(f"Unknown tag: {name!r}")
            if replacement is None:
                self.channel_catalog.require_unreferenced_tag(name)
            else:
                self.channel_catalog.require_available_tag_name(replacement, previous=name)

            def changed(tags: frozenset[str]) -> frozenset[str]:
                return (tags - {name}) | ({replacement} if name in tags and replacement else set())

            for thread in self.registry.all_threads().values():
                if name in thread.tags:
                    self.registry.register(
                        replace(thread, tags=changed(thread.tags)),
                        self.registry.status(thread.name),
                    )
            tags, channels = self.channel_catalog.read()
            updated_channels: dict[str, Channel] = {}
            previous_target = f"#{name}"
            replacement_target = f"#{replacement}" if replacement else None
            for key, channel in channels.items():
                if selected := changed(channel.tags):
                    target = (
                        replacement_target
                        if replacement_target and key == previous_target and channel.exact
                        else key
                    )
                    updated_channels[target] = replace(channel, name=target, tags=selected)
                else:
                    self.channel_catalog.remove_channel(key)
            if replacement_target:
                self.channel_catalog.rename_tag_channel(previous_target, replacement_target)
            else:
                self.channel_catalog.remove_channel(previous_target)
            self.channel_catalog.change_tag_metadata(name, replacement)
            self.channel_catalog.write(changed(tags), updated_channels)

    def who(self) -> Sequence[Mapping]:
        """Presence: who is in the chat, with status and unread counts."""
        return self._presence(include_pending=True)

    def presence(self) -> Sequence[Mapping]:
        """Presence without viewer-specific unread scans."""
        return self._presence(include_pending=False)

    def _presence(self, *, include_pending: bool) -> Sequence[Mapping[str, object]]:
        return [
            {
                **view.to_wire(),
                **({"pending": self.pending_count(view.thread.name)} if include_pending else {}),
            }
            for view in sorted(self.thread_views(), key=lambda view: view.thread.name)
        ]

    def thread_transcript(
        self,
        name: str,
        *,
        max_messages: int = 20,
        max_bytes: int = 64 * 1024,
    ) -> Sequence[TranscriptEvent]:
        """Return a bounded normalized tail of one thread's Pi session transcript."""
        thread, session_file, inherited = self._thread_transcript_source(name)
        if max_messages <= 0 or max_bytes <= 0:
            return ()
        path = Path(session_file)
        try:
            size = path.stat().st_size if session_file else 0
        except OSError:
            size = 0

        records: list[list[TranscriptEvent]] = []
        with self.transcript_routes.for_session(session_file) as routes:
            for raw_line in _reverse_lines(path, max_bytes=max_bytes) if session_file else ():
                try:
                    payload = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                message = payload.get("message")
                events = self._transcript_record_events(
                    payload,
                    routes.get(payload.get("id", "")),
                    (
                        routes.input_display(message.get("inputId"))
                        if isinstance(message, Mapping)
                        else None
                    ),
                )
                if events:
                    records.append(events)
                    if len(records) > max_messages:
                        break

        truncated = size > max_bytes or len(records) > max_messages
        records = list(reversed(records[:max_messages]))
        events = [event for record in records for event in record]
        if truncated:
            events.insert(
                0,
                TranscriptEvent(
                    "notice",
                    "Earlier transcript content was omitted from this bounded view.",
                ),
            )
        if inherited:
            events.extend(self._fork_start_events(thread))
        return tuple(events)

    def _thread_transcript_source(
        self, name: str, source_file: str | None = None
    ) -> tuple[Thread, str, bool]:
        """Resolve a temporary inherited view without changing runtime ownership."""
        thread = self.registry.require(name)
        if thread.session_file and (source_file is None or source_file == thread.session_file):
            return thread, thread.session_file, False
        ancestor = thread
        visited = {thread.name}
        while ancestor.parent is not None:
            ancestor = self.registry.require(ancestor.parent)
            if ancestor.name in visited:
                break
            visited.add(ancestor.name)
            if ancestor.session_file and (
                source_file is None or source_file == ancestor.session_file
            ):
                return thread, ancestor.session_file, True
        if source_file:
            raise ValueError("Transcript changed; reload the latest page.")
        return thread, "", bool(thread.parent)

    @staticmethod
    def _fork_start_events(thread: Thread) -> tuple[TranscriptEvent, ...]:
        if not thread.parent:
            return ()
        return (
            TranscriptEvent("notice", f"Forked from @{thread.parent}. This thread started with:"),
            *((TranscriptEvent("user", thread.task),) if thread.task else ()),
        )

    def thread_transcript_page(
        self,
        name: str,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        max_messages: int = 20,
        max_bytes: int = 64 * 1024,
        through: TranscriptCursor | None = None,
    ) -> TranscriptPage:
        """Read one adjacent page with exclusive, file-bound byte cursors."""
        if before is not None and after is not None:
            raise ValueError("Choose one transcript paging direction.")
        if max_messages <= 0 or max_bytes <= 0:
            raise ValueError("Transcript page budgets must be positive.")
        # A mounted inherited window remains pinned to its ancestor and byte
        # boundary when the child persists its own session. New unpinned reads
        # select the child's file; existing scroll cursors keep working.
        thread, session_file, inherited = self._thread_transcript_source(
            name, through.session_file if through is not None else None
        )
        cursor = before or after
        if cursor and (cursor.session_file != session_file or cursor.offset < 0):
            raise ValueError("Transcript changed; reload the latest page.")
        path = Path(session_file)
        size = path.stat().st_size if session_file and path.is_file() else 0
        if cursor and cursor.offset > size:
            raise ValueError("Transcript changed; reload the latest page.")
        if through is not None:
            if through.session_file != session_file or not 0 <= through.offset <= size:
                raise ValueError("Transcript changed; reload the latest page.")
            size = through.offset
            if cursor and cursor.offset > size:
                raise ValueError("Cursor is outside the transcript window.")
        start = end = cursor.offset if cursor else size
        records: list[tuple[TranscriptEvent, ...]] = []
        routes = self.transcript_routes.for_session(session_file)
        used = 0

        def forward() -> Generator[tuple[int, int, bytes], None, None]:
            with path.open("rb") as stream:
                stream.seek(start)
                while True:
                    offset = stream.tell()
                    if offset >= size:
                        return
                    raw = stream.readline()
                    if not raw:
                        return
                    yield offset, stream.tell(), raw

        with routes:
            if size:
                iterator = forward() if after else _reverse_records(path, start)
                try:
                    for record_start, record_end, raw in iterator:
                        try:
                            value = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            # A writer may have left an incomplete last line. Retry it
                            # after the next append rather than losing its cursor.
                            if after and record_end == size and not raw.endswith(b"\n"):
                                break
                            events: tuple[TranscriptEvent, ...] = ()
                        else:
                            events = (
                                tuple(
                                    self._transcript_record_events(
                                        value,
                                        routes.get(value.get("id", "")),
                                        (
                                            routes.input_display(value["message"].get("inputId"))
                                            if isinstance(value.get("message"), Mapping)
                                            else None
                                        ),
                                    )
                                )
                                if isinstance(value, dict)
                                else ()
                            )
                        if (
                            events
                            and records
                            and (len(records) >= max_messages or used + len(raw) > max_bytes)
                        ):
                            break
                        if after:
                            end = record_end
                        else:
                            start = record_start
                        if events:
                            records.append(events)
                            used += len(raw)
                finally:
                    iterator.close()
        if not after:
            records.reverse()
        events = tuple(event for record in records for event in record)
        if inherited and not after and (cursor is None or cursor.offset == size):
            events = (*events, *self._fork_start_events(thread))
        return TranscriptPage(
            events,
            TranscriptCursor(session_file, start),
            TranscriptCursor(session_file, end),
            start > 0,
            end < size,
        )

    def _transcript_record_events(
        self,
        payload: Mapping[str, object],
        routing: TurnRouting | None = None,
        input_display: InputDisplay | None = None,
    ) -> list[TranscriptEvent]:
        if payload.get("type") == "compaction":
            summary = str(payload.get("summary") or "").strip()
            if summary:
                return [TranscriptEvent("notice", f"## Context compacted\n\n{summary}")]
            return []
        message = payload.get("message")
        if payload.get("type") != "message" or not isinstance(message, Mapping):
            return []
        return self._transcript_message_events(message, routing, input_display)

    def _transcript_message_events(
        self,
        message: Mapping[str, object],
        routing: TurnRouting | None = None,
        input_display: InputDisplay | None = None,
    ) -> list[TranscriptEvent]:
        role = message.get("role")
        if (
            role == "user"
            and input_display is not None
            and input_display.sent_text_digest is not None
        ):
            raw_content = message.get("content")
            raw_text = (
                raw_content
                if isinstance(raw_content, str)
                else (
                    "\n".join(
                        str(part.get("text") or "")
                        for part in raw_content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    if isinstance(raw_content, list)
                    else None
                )
            )
            if raw_text is not None and input_display.matches(raw_text):
                # Exact per-input provenance wins over old turn-wide annotations,
                # including explicitly bound human/internal inputs (no route).
                routing = input_display.routing
            else:
                routing = None
                input_display = None
        if role == "user" and routing is not None and routing.requests:
            return [
                TranscriptEvent("user", request.body, routing=TurnRouting((request,), None))
                for request in routing.requests
            ]
        if role == "assistant" and message.get("stopReason") in {"error", "aborted"}:
            failure = str(message.get("errorMessage") or "").strip()
            if failure:
                return [TranscriptEvent("notice", f"[agent error] {failure}")]
        content = message.get("content")
        if isinstance(content, str):
            parts: Sequence[object] = ({"type": "text", "text": content},)
        elif isinstance(content, list):
            parts = content
        else:
            return []

        context_events: list[TranscriptEvent] = []
        if role == "user" and input_display is not None:
            raw_text = "\n".join(
                str(part.get("text") or "")
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if raw_text and raw_text != input_display.text:
                context_events.append(TranscriptEvent("context", raw_text))
            if input_display.text is None:
                return context_events
            # The owner records the user's original text before adding model-only
            # instructions. Preserve attachments while replacing just that text.
            parts = (
                {"type": "text", "text": input_display.text},
                *(part for part in parts if isinstance(part, dict) and part.get("type") != "text"),
            )

        events: list[TranscriptEvent] = context_events
        for part in parts:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if role == "user" and kind == "text":
                events.append(TranscriptEvent("user", str(part.get("text") or "")))
            elif role == "user" and kind == "image":
                events.append(
                    TranscriptEvent("user", f"[Image attachment: {part.get('mimeType', 'image')}]")
                )
            elif role == "assistant" and kind == "thinking":
                events.append(TranscriptEvent("thinking", str(part.get("thinking") or "")))
            elif role == "assistant" and kind == "text":
                text = str(part.get("text") or "")
                if events and events[-1].kind == "assistant":
                    events[-1] = replace(events[-1], text=events[-1].text + text)
                else:
                    events.append(TranscriptEvent("assistant", text))
            elif role == "assistant" and kind == "toolCall":
                events.append(
                    TranscriptEvent(
                        "tool_start",
                        tool_call_id=str(part.get("id") or ""),
                        tool_name=str(part.get("name") or "tool"),
                        raw_input=part.get("arguments"),
                    )
                )
            elif role == "toolResult":
                output = "\n".join(
                    str(item.get("text") or "")
                    for item in parts
                    if isinstance(item, dict) and item.get("type") == "text"
                )
                result = [
                    TranscriptEvent(
                        "tool_end",
                        text=output,
                        tool_call_id=str(message.get("toolCallId") or ""),
                        tool_name=str(message.get("toolName") or "tool"),
                        ok=not bool(message.get("isError")),
                        diff=ToolDiff.from_result(
                            str(message.get("toolName") or "tool"),
                            message,
                            not bool(message.get("isError")),
                        ),
                    )
                ]
                sent = self.sent_tool_message(
                    str(message.get("toolName") or ""), output, not bool(message.get("isError"))
                )
                if sent is not None:
                    result.append(
                        TranscriptEvent(
                            "sent",
                            sent.body,
                            routing=TurnRouting(reply=MessageRoute(sent.sender, (sent.target,))),
                        )
                    )
                return result
        return [replace(event, routing=routing) for event in events]

    def transcript_checkpoint(self, name: str) -> TranscriptCursor:
        session_file = self.registry.require(name).session_file or ""
        path = Path(session_file)
        return TranscriptCursor(
            session_file, path.stat().st_size if session_file and path.is_file() else 0
        )

    def record_input_display(
        self,
        native_id: str,
        display_text: str | None,
        *,
        sent_text: str | None = None,
        routing: TurnRouting | None = None,
    ) -> None:
        """Bind UI text to the private native input ID, never a prompt prefix."""
        self.transcript_routes.record_input_display(
            native_id, display_text, sent_text=sent_text, routing=routing
        )

    def record_turn_routing(
        self, name: str, checkpoint: TranscriptCursor, routing: TurnRouting
    ) -> None:
        session_file = self.registry.require(name).session_file
        if not session_file or not Path(session_file).is_file():
            return
        ids: list[str] = []
        if session_file == checkpoint.session_file:
            with Path(session_file).open("rb") as stream:
                stream.seek(checkpoint.offset)
                for raw in stream:
                    record = json.loads(raw)
                    if record.get("type") == "message" and isinstance(record.get("id"), str):
                        ids.append(record["id"])
        else:
            # A first turn or fork may create a new Pi file. Only annotate the
            # completed final assistant entry, never inherited entries by guess.
            for raw in _reverse_lines(Path(session_file)):
                record = json.loads(raw)
                if (
                    record.get("type") == "message"
                    and record.get("message", {}).get("role") == "assistant"
                ):
                    if isinstance(record.get("id"), str):
                        ids.append(record["id"])
                    break
        self.transcript_routes.record(session_file, tuple(ids), routing)

    def repair_input_routing(self, *, dry_run: bool = True) -> dict[str, int | bool]:
        """Explicit maintenance for old receipt-bound inputs, never a UI/wake scan.

        Join committed envelopes by sequence to owner-persisted native ID/text
        bindings. A prompt prefix or a matching body alone is not evidence.
        No transcript, input disposition, delivery/read cursor, or model is changed.
        """
        from .declarations import ScheduledTurn
        from .input_disposition import InputDispositions

        rows = InputDispositions(self.root).bound_bus_inputs()
        existing = self.transcript_routes.input_bindings()
        groups: dict[str, list[dict[str, Any]]] = {}
        needed = {row["sequence"] for row in rows}
        envelopes: dict[int, Message] = {}
        aliases = self.registry.snapshot().aliases
        report: dict[str, int | bool] = {
            "dry_run": dry_run,
            "eligible": 0,
            "already_bound": 0,
            "repaired": 0,
            "skipped": 0,
            "conflicts": 0,
        }
        for row in rows:
            groups.setdefault(row["native_id"], []).append(row)
        with self.bus.full_history_snapshot() as (_, messages):
            for message in messages:
                if message.seq in needed:
                    envelopes[message.seq] = message
        for native_id, group in groups.items():
            group.sort(key=lambda row: row["sequence"])
            proof = {
                (row["owner"], row["admission"], row["turn_id"], row["sent_text"]) for row in group
            }
            if (
                len(proof) != 1
                or len({row["sequence"] for row in group}) != len(group)
                or (len(group) > 1 and not all(is_channel_target(row["target"]) for row in group))
            ):
                report["conflicts"] += 1
                continue
            origins: list[Message] = []
            for row in group:
                candidate = envelopes.get(row["sequence"])
                if (
                    candidate is None
                    or row["target"] != candidate.target
                    or row["source_text"]
                    not in {
                        ScheduledTurn.incoming(candidate).prompt,
                        ScheduledTurn.incoming(candidate, aliases=aliases).prompt,
                    }
                ):
                    break
                origins.append(candidate)
            source = "\n\n".join(row["source_text"] for row in group)
            sent_text = group[0]["sent_text"]
            if len(origins) != len(group) or not sent_text.endswith(source):
                report["skipped"] += 1
                continue
            routing = TurnRouting(tuple(origins), None)
            binding = (
                hashlib.sha256(sent_text.encode("utf-8")).hexdigest(),
                json.dumps(routing.to_wire(), sort_keys=True),
            )
            if native_id in existing:
                report["already_bound" if existing[native_id] == binding else "conflicts"] += 1
                continue
            report["eligible"] += 1
            if not dry_run:
                try:
                    self.record_input_display(
                        native_id, source, sent_text=sent_text, routing=routing
                    )
                except RelationViolationError:
                    report["conflicts"] += 1
                else:
                    report["repaired"] += 1
        return report

    # ─── Threads ──────────────────────────────────────────────────────────────

    def import_thread(
        self,
        source: Path | str,
        format: ImportFormat,
        *,
        name: str,
        session_id: str | None = None,
        worktree: str | None = None,
        model: str | None = None,
        tags: frozenset[str] = frozenset(),
        limits: ImportLimits | None = None,
    ) -> ImportReceipt:
        """Import a stopped, resumable snapshot without adopting a source executor."""
        source_path = Path(source).expanduser().resolve()
        snapshot = format.read(source_path, limits or ImportLimits(), session_id)
        directory = worktree or snapshot.project
        if not directory:
            raise ValueError("The source has no project directory; supply --worktree.")
        project = Path(directory).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("The imported project does not exist locally; supply --worktree.")
        session_path = self.root / "imported_sessions" / f"{uuid4().hex}.jsonl"
        thread = Thread(
            name,
            tags,
            str(project),
            session_file=str(session_path),
            title=snapshot.title or name,
            model=model,
        )
        with _store_lock(self._wire_lock_path):
            if self.registry.name_reserved(name):
                raise ValueError(f"Thread name {name!r} is already reserved.")
            self._require_available_new_tags(thread.tags)
            session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix":
                # Do not follow a redirected legacy directory when repairing it.
                info = session_path.parent.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                    raise ValueError("Imported session directory is not owner-controlled.")
                # Native Pi requires a private session directory before it will
                # attest input IDs. Also repair directories made by older imports.
                session_path.parent.chmod(0o700)
            _atomic_write_text(session_path, snapshot.pi_session(project))
            try:
                self.registry.register(thread, ThreadStatus.STOPPED)
                self.bus.mark_delivered_through(name, self.message_high_water())
            except Exception:
                if name in self.registry:
                    self.registry.remove(name)
                session_path.unlink(missing_ok=True)
                raise
        return ImportReceipt(
            name,
            str(session_path),
            format,
            snapshot.source_id,
            len(snapshot.messages),
            snapshot.messages_seen - len(snapshot.messages),
            snapshot.truncated_messages,
            snapshot.notices,
        )

    def register(self, thread: Thread) -> None:
        """Declare a thread in the registry.

        Re-declaring an existing thread cannot silently drop provenance:
        empty tags inherit the prior declaration's tags (a child that does
        not receive tag env still keeps its channel subscriptions), and a
        missing session_file and model keep the prior declaration. Explicit
        values always win.
        """
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.canonical_name(thread.name)
            existing = self.registry.all_threads().get(canonical)
            if existing is not None and existing.executing:
                if thread.pid not in {0, existing.pid}:
                    raise RelationViolationError(
                        "Cannot replace an executor during its active turn."
                    )
                thread = replace(
                    thread,
                    pid=existing.pid,
                    active_turn=existing.active_turn,
                    turn_generation=existing.turn_generation,
                    last_finished_turn_id=existing.last_finished_turn_id,
                )
            tags = thread.tags
            session_file = thread.session_file
            model = thread.model
            thinking_level = thread.thinking_level
            goal = thread.goal
            previous_worktrees = thread.previous_worktrees
            auto_title_pending = (
                existing.auto_title_pending if existing is not None else thread.auto_title_pending
            )
            title = (
                thread.title if thread.title is not None else existing.title if existing else None
            )
            if existing is not None:
                if not tags:
                    tags = existing.tags
                if session_file is None:
                    session_file = existing.session_file
                if model is None:
                    model = existing.model
                if thinking_level is None:
                    thinking_level = existing.thinking_level
                if goal is None:
                    goal = existing.goal
                if not previous_worktrees:
                    previous_worktrees = existing.previous_worktrees
            if (
                canonical != thread.name
                or tags != thread.tags
                or session_file != thread.session_file
                or model != thread.model
                or thinking_level != thread.thinking_level
                or goal != thread.goal
                or previous_worktrees != thread.previous_worktrees
                or auto_title_pending != thread.auto_title_pending
                or title != thread.title
            ):
                thread = replace(
                    thread,
                    name=canonical,
                    tags=tags,
                    session_file=session_file,
                    model=model,
                    thinking_level=thinking_level,
                    goal=goal,
                    previous_worktrees=previous_worktrees,
                    auto_title_pending=auto_title_pending,
                    title=title,
                )
            self._require_available_new_tags(thread.tags)
            # A fresh public registration is a new owner admission even when
            # the OS has reused its PID and the project path is unchanged.
            # Metadata setters re-register the saved declaration internally.
            new_owner = (
                existing is not None
                and existing.pid > 0
                and thread.pid > 0
                and thread.created_at != existing.created_at
            )
            self.registry.register(thread, new_owner=new_owner)

            self.channel_catalog.remember_tags(thread.tags, thread.created_at)
            if existing is not None and existing.tags != thread.tags:
                self._rebase_passive_channel_scope(thread.name)

    def claim_thread(
        self,
        base_name: str,
        *,
        tags: frozenset[str],
        worktree: str,
        pid: int = 0,
        start_at_latest: bool = False,
        model: str | None = None,
        thinking_level: str | None = None,
        auto_title_pending: bool = False,
    ) -> Thread:
        """Atomically register a unique thread and optionally baseline its inbox."""
        with _store_lock(self._wire_lock_path):
            name = base_name
            suffix = 2
            while self.registry.name_reserved(name):
                name = f"{base_name}-{suffix}"
                suffix += 1
            thread = Thread(
                name=name,
                tags=tags,
                worktree=worktree,
                pid=pid,
                model=model,
                thinking_level=thinking_level,
                auto_title_pending=auto_title_pending,
            )
            self._require_available_new_tags(thread.tags)
            self.registry.register(thread)
            if start_at_latest:
                self.bus.mark_delivered_through(thread.name, self.bus.latest_sequence())
            return thread

    def rename_self(self, new_name: str) -> RenameThreadResult:
        """Rename the caller's own running thread, retaining its old aliases."""
        caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
        if not caller:
            raise RelationViolationError("Self rename requires PI_AGENT_ID or AGENT_COMMS_THREAD.")
        with _store_lock(self._wire_lock_path):
            if self.registry.require(caller).auto_title_pending and (
                len(new_name) > 48 or len(re.split(r"[-_]+", new_name)) > 6
            ):
                raise ValueError("Choose a concise topic title: at most 6 words and 48 characters.")
            return self._rename_thread(
                caller, new_name, title=new_name.replace("-", " ").replace("_", " ")
            )

    def set_project_self(self, path: str) -> ProjectChangeResult:
        caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
        if not caller:
            raise RelationViolationError("Changing your project requires a thread identity.")
        return self.set_project(caller, path)

    def set_project(self, name: str, path: str) -> ProjectChangeResult:
        """Move one thread's project, preserving its owner and conversation."""
        if not path.strip():
            raise ValueError("Project path cannot be empty.")
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = Path(thread.worktree) / target
            try:
                target = target.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"Cannot open project directory: {target}") from error
            if not target.is_dir():
                raise ValueError(f"Project path is not a directory: {target}")
            previous = str(Path(thread.worktree).expanduser().resolve())
            current = str(target)
            if current == previous:
                return ProjectChangeResult(thread.name, previous, current, False)
            history = tuple(dict.fromkeys((*thread.previous_worktrees, previous)))
            self.registry.register(
                replace(thread, worktree=current, previous_worktrees=history),
                self.registry.status(thread.name),
            )
            return ProjectChangeResult(thread.name, previous, current, True)

    def rename_managed_thread(
        self, name: str, display_name: str, *, owner_pid: int
    ) -> RenameThreadResult:
        """Rename a locally managed running thread after proving process ownership."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            if owner_pid <= 0 or thread.pid != owner_pid:
                raise RelationViolationError(
                    f"Process {owner_pid} does not own thread {thread.name!r}."
                )
            base_name = re.sub(r"[^A-Za-z0-9_-]+", "-", display_name).strip("-_") or "session"

            new_name = base_name
            suffix = 2
            while self.registry.name_reserved(new_name):
                if self.registry.canonical_name(new_name) == thread.name:
                    return self._rename_thread(thread.name, new_name, title=display_name)
                new_name = f"{base_name}-{suffix}"
                suffix += 1
            return self._rename_thread(thread.name, new_name, title=display_name)

    def _rename_thread(
        self, name: str, new_name: str, *, title: str | None = None
    ) -> RenameThreadResult:
        previous, current = self.registry.rename(name, new_name)
        thread = self.registry.require(current)
        if thread.auto_title_pending or title is not None:
            self.registry.register(
                replace(thread, auto_title_pending=False, title=title or thread.title),
                self.registry.status(current),
            )
        if previous == current:
            return RenameThreadResult(previous, current, False)
        self.bus.rename_thread(previous, current)
        self.activity.rename_thread(previous, current)
        self.runtime_info.rename_thread(previous, current)
        self.ledger.rename_thread(previous, current)
        self.channel_catalog.rename_thread(previous, current)
        return RenameThreadResult(previous, current, True)

    def goal_pause(self, name: str) -> GoalPauseEvent | None:
        """Return the action that paused this exact current goal revision, if known."""
        events = GoalPauseEvents(self.root / "goal_pause_events.json")
        return events.for_goal(self.registry.require(name).goal, events.snapshot())

    def unresolved_inputs(self, name: str) -> list[dict[str, Any]]:
        """Project durable unresolved inputs; reading never schedules another attempt."""
        from .input_disposition import InputDispositions

        with _store_lock(self._wire_lock_path):
            self.registry.require(name)
            rows = InputDispositions(self.root).unknown(self.registry.aliases_for(name))
            return [InputDispositions.public(row) for row in rows]

    def input_delivery(
        self,
        name: str,
        *,
        include_history: bool = False,
        awaiting_keys: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Read current delivery notices and separately counted migration history."""
        from .input_disposition import AcpDeliveryCursors, InputDispositions

        with _store_lock(self._wire_lock_path):
            self.registry.require(name)
            aliases = self.registry.aliases_for(name)
            boundary = AcpDeliveryCursors(self.root).legacy_through(aliases)
            return InputDispositions(self.root).delivery_overview(
                aliases, boundary, include_history=include_history, awaiting_keys=awaiting_keys
            )

    def dismiss_historical_inputs(
        self, name: str, *, awaiting_keys: frozenset[str] | None = None
    ) -> dict[str, Any]:
        """Clear only migration notices; UNKNOWN remains unresolved and unreplayable."""
        from .input_disposition import AcpDeliveryCursors, InputDispositions

        with _store_lock(self._wire_lock_path):
            self.registry.require(name)
            aliases = self.registry.aliases_for(name)
            boundary = AcpDeliveryCursors(self.root).legacy_through(aliases)
            return InputDispositions(self.root).dismiss_historical(
                aliases, boundary, awaiting_keys=awaiting_keys
            )

    def goal_input_review(self, name: str, goal_id: str, wait_for: Sequence[str]) -> dict:
        """Project exact review eligibility for one current goal and dependency set."""
        with _store_lock(self._wire_lock_path):
            return self._goal_input_review(self.registry.require(name), goal_id, wait_for).public()

    def _goal_input_review(
        self, thread: Thread, goal_id: str, wait_for: Sequence[str]
    ) -> GoalInputReview:
        from .input_disposition import InputDispositions

        goal = thread.goal
        if goal is None or goal.id != goal_id:
            raise ValueError("This goal was replaced or cleared; refresh its state.")
        if not goal.active:
            raise ValueError(
                (pause.owner_instruction if (pause := self.goal_pause(thread.name)) else None)
                or "This goal is no longer active; refresh its state."
            )
        if not wait_for:
            raise ValueError("Standby requires explicit wait_for thread names.")
        resolved = tuple(self.registry.require(target.removeprefix("@")) for target in wait_for)
        if any(target.created_at == thread.created_at for target in resolved):
            raise ValueError("A goal cannot wait for its own thread.")
        targets = tuple(
            dict.fromkeys(GoalWaitTarget(target.name, target.created_at) for target in resolved)
        )
        owners = self.registry.aliases_for(thread.name)
        senders = frozenset(
            alias for target in resolved for alias in self.registry.aliases_for(target.name)
        )
        unknown = tuple(InputDispositions(self.root).unknown(owners))
        sequences = {row["sequence"] for row in unknown if row["sequence"] is not None}
        eligible = set()
        if sequences:
            selected = self.bus._history_page(
                lambda message: message.seq in sequences
                and message.target in owners
                and message.sender in senders,
                before=None,
                after=min(sequences) - 1,
                limit=len(sequences),
                max_bytes=max(256 * 1024, sum(len(json.dumps(row).encode()) for row in unknown)),
            )
            eligible = {message.seq for message in selected.messages}
        return GoalInputReview(
            goal_id,
            targets,
            owners,
            senders,
            unknown,
            frozenset(row["key"] for row in unknown if row["sequence"] in eligible),
        )

    def goal_history(
        self, name: str, *, goal_id: str | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read durable revisions for one thread incarnation and optional goal ID."""
        with _store_lock(self._wire_lock_path):
            return self.registry.goal_history(name, goal_id=goal_id)

    def goal_wait(self, name: str) -> GoalWait | None:
        waits = GoalWaits(self.root / "goal_waits.json")
        return waits.for_goal(self.registry.require(name).goal, waits.snapshot())

    def recover_closed_goal_wait(self, name: str) -> tuple[str, ...]:
        """Release one stranded standby without replaying a dependency input.

        This is an owner-side liveness check, not an agent turn. A subsequent
        scheduler pass may continue the still-active goal only if its private
        attempt ledger has a fresh READY grant.
        """
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            canonical = snapshot.aliases.get(name, name)
            owner = snapshot.threads.get(canonical)
            if owner is None or owner.active_turn is not None:
                return ()
            goal = owner.goal
            if goal is None or not goal.active:
                return ()
            waits = GoalWaits(self.root / "goal_waits.json")
            rows = waits.snapshot()
            wait = rows.get(goal.id)
            if (
                wait is None
                or wait.owner_created_at not in (None, owner.created_at)
                or wait.revision > goal.revision
            ):
                return ()
            closed = GoalWaits.closed_wait_group(
                canonical, wait.targets, rows, snapshot, self._process_alive
            )
            if not closed:
                return ()
            owner_aliases = frozenset(
                {
                    canonical,
                    *(alias for alias, target in snapshot.aliases.items() if target == canonical),
                }
            )
            try:
                reply = self.bus._history_page(
                    lambda message: message.target in owner_aliases
                    and message.starts_turn_for(canonical, aliases=snapshot.aliases)
                    and wait.matches(message, snapshot),
                    before=None,
                    after=wait.after_seq,
                    limit=1,
                    max_bytes=256 * 1024,
                    targets=owner_aliases,
                )
            except (OSError, ValueError, sqlite3.DatabaseError):
                # An unavailable read cannot prove that no reply was delivered.
                return ()
            if reply.messages:
                return ()
            names = ", ".join(f"@{member}" for member in closed)
            note = (
                f"Dependency wait group ({names}) has no independent worker. "
                "Standby was released; inspect dependencies and continue useful work."
            )
            progress = f"{goal.progress}\n\n{note}" if goal.progress else note
            self.registry.register(
                replace(owner, goal=replace(goal, progress=progress, revision=goal.revision + 1)),
                snapshot.statuses[canonical],
            )
            if not waits.clear(goal.id, wait_id=wait.wait_id):
                return ()
            return closed

    def release_waits_after_terminal_turn(self, fence: FinishedTurnFence | None) -> tuple[str, ...]:
        """Release waits after the latest exact, still-idle child turn.

        ACP invokes this after terminal publication, never at the earlier UI
        `settled` event. A later turn (even already finished) invalidates the
        old fence; a rename or unrelated metadata edit does not.
        """
        if type(fence) is not FinishedTurnFence:
            return ()
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            canonical = snapshot.aliases.get(fence.name, fence.name)
            source = snapshot.threads.get(canonical)
            if (
                source is None
                or source.created_at != fence.created_at
                or source.active_turn is not None
                or source.turn_generation != fence.turn_generation
                or source.last_finished_turn_id != fence.turn_id
                or snapshot.admission_generations.get(canonical) != fence.admission_generation
                or not snapshot.statuses[canonical].active
            ):
                return ()
            waits = GoalWaits(self.root / "goal_waits.json").snapshot()
            released: list[str] = []
            for owner in snapshot.threads.values():
                goal = owner.goal
                if goal is None or not goal.active:
                    continue
                wait = waits.get(goal.id)
                if (
                    wait is None
                    or wait.owner_created_at != owner.created_at
                    or wait.revision > goal.revision
                    or len(wait.target_turn_generations) != len(wait.targets)
                    or not any(
                        snapshot.aliases.get(target.name, target.name) == canonical
                        and target.created_at == fence.created_at
                        and (generation := wait.target_turn_generations[index]) is not None
                        and type(generation) is int
                        and 0 < generation <= fence.turn_generation
                        for index, target in enumerate(wait.targets)
                    )
                    or any(
                        GoalWaits.target_has_active_turn(target, snapshot)
                        for target in wait.targets
                    )
                ):
                    continue
                owner_aliases = frozenset(
                    {
                        owner.name,
                        *(
                            alias
                            for alias, target in snapshot.aliases.items()
                            if target == owner.name
                        ),
                    }
                )

                def qualifies_direct_reply(
                    message: Message,
                    *,
                    aliases: frozenset[str] = owner_aliases,
                    owner_name: str = owner.name,
                    current_wait: GoalWait = wait,
                ) -> bool:
                    return (
                        message.target in aliases
                        and message.starts_turn_for(owner_name, aliases=snapshot.aliases)
                        and current_wait.matches(message, snapshot)
                    )

                try:
                    reply = self.bus._history_page(
                        qualifies_direct_reply,
                        before=None,
                        after=wait.after_seq,
                        limit=1,
                        max_bytes=256 * 1024,
                        targets=owner_aliases,
                    )
                except (OSError, ValueError, sqlite3.DatabaseError):
                    # The terminal turn has already committed. An unavailable
                    # optional reply read cannot prove silence or release this
                    # owner; do not turn the completed ACP turn into a failure.
                    # Registry/goal writes below remain outside this guard.
                    continue
                if reply.messages:
                    continue
                diagnostic = (
                    f"Declared dependency @{canonical} finished without a qualifying direct "
                    "reply, and no declared dependency has an active turn. "
                    "Standby was released; inspect messages and UNKNOWN inputs, then "
                    "continue independent work or redelegate. No input was replayed."
                )
                progress = f"{goal.progress}\n\n{diagnostic}" if goal.progress else diagnostic
                # Persist the explanation before clearing the wait. A crash
                # between these writes leaves the active goal in standby;
                # the periodic closed-wait check can release it later.
                continued_goal = replace(goal, progress=progress, revision=goal.revision + 1)
                self.registry.register(
                    replace(owner, goal=continued_goal), snapshot.statuses[owner.name]
                )
                GoalWaits(self.root / "goal_waits.json").clear(goal.id, wait_id=wait.wait_id)
                released.append(owner.name)
            return tuple(released)

    def goal_execution(self, name: str) -> GoalExecution | None:
        return self._goal_snapshot(name)[1]

    def goal_snapshot(self, name: str) -> tuple[Goal | None, GoalExecution | None]:
        """Read current goal and its scheduling projection as one owner snapshot."""
        with _store_lock(self._wire_lock_path):
            return self._goal_snapshot(name)

    def _goal_snapshot(self, name: str) -> tuple[Goal | None, GoalExecution | None]:
        snapshot = self.registry.snapshot()
        canonical = snapshot.aliases.get(name, name)
        goal = snapshot.threads[canonical].goal
        return goal, GoalWaits.execution(
            goal,
            GoalWaits(self.root / "goal_waits.json").snapshot(),
            snapshot,
        )

    def consume_goal_wait(self, name: str, wait_id: str) -> bool:
        """Called under the send-boundary wire lock after reserving an attempt."""
        goal = self.registry.require(name).goal
        return bool(
            goal is not None
            and goal.active
            and GoalWaits(self.root / "goal_waits.json").clear(goal.id, wait_id=wait_id)
        )

    def list_threads(self, active_only: bool = False) -> Sequence[Mapping]:
        """Summarize threads with status and pending counts."""
        snapshot = self.registry.snapshot()
        threads = {
            name: thread
            for name, thread in snapshot.threads.items()
            if not active_only or snapshot.statuses[name].active
        }
        activities = self.activity.all_current()
        pending = self.bus.pending_counts_all(tuple(threads))
        pause_events = GoalPauseEvents(self.root / "goal_pause_events.json").snapshot()
        waits = GoalWaits(self.root / "goal_waits.json").snapshot()
        return [
            {
                **t.to_wire(),
                "status": snapshot.statuses[name].value,
                "is_fork": t.is_fork,
                "pending": pending[name],
                "goal_pause": (
                    asdict(pause)
                    if (pause := GoalPauseEvents.for_goal(t.goal, pause_events))
                    else None
                ),
                "goal_execution": (
                    asdict(execution)
                    if (execution := GoalWaits.execution(t.goal, waits, snapshot))
                    else None
                ),
                "activity": activities[name].state.value if name in activities else "idle",
                "activity_detail": activities[name].detail if name in activities else "",
            }
            for name, t in sorted(threads.items())
        ]

    def thread_detail(self, name: str, *, include_pending: bool = True) -> Mapping[str, object]:
        t = self.registry.require(name)
        detail = {**t.to_wire(), "status": self.registry.status(name).value, "is_fork": t.is_fork}
        execution = self.goal_execution(name)
        detail["goal_execution"] = asdict(execution) if execution else None
        if include_pending:
            detail["pending"] = self.pending_count(name)
        return detail

    def heartbeat(self, name: str) -> None:
        with _store_lock(self._wire_lock_path):
            self.registry.heartbeat(name)

    def set_thread_model(self, name: str, model: str) -> Thread:
        """Persist the model selected for every future turn of a thread."""
        selected = model.strip()
        if not selected:
            raise ValueError("Thread model cannot be empty.")
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            updated = replace(thread, model=selected)
            self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def set_thread_thinking_level(self, name: str, level: str) -> Thread:
        """Persist Pi's thinking level for every future turn of a thread."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            updated = replace(thread, thinking_level=level)
            self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def resolve_thread_model(self, name: str, fallback: str | None = None) -> str | None:
        """Prefer a saved selection, then the resumed session's last model."""
        thread = self.registry.require(name)
        if thread.model is not None:
            return thread.model
        if thread.session_file and (model := _session_model(Path(thread.session_file))):
            return "/".join(model)
        info = self.agent_info_of(thread.name)
        return (info.model if info else None) or fallback

    def update_goal(
        self,
        name: str,
        action: str,
        *,
        text: str = "",
        progress: str | None = None,
        block_reason: str | None = None,
        goal_id: str | None = None,
        expected_status: str | None = None,
        expected_goal: Goal | None = None,
        model_report: bool = False,
        owner_action: bool = False,
        owner_store: GoalAttemptStore | None = None,
        expected_owner_pid: int | None = None,
        wait_for: Sequence[str] = (),
        reviewed_inputs: Sequence[str] = (),
    ) -> Goal | None:
        """Apply a goal transition; automated callers may compare a captured goal atomically."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            goal = thread.goal
            if expected_owner_pid is not None and (
                thread.pid != expected_owner_pid or not self.registry.status(thread.name).running
            ):
                raise ValueError("The goal owner changed; refresh its state.")
            if (
                owner_store is not None
                and action != "set"
                and not (action == "active" and owner_action and expected_owner_pid is not None)
            ):
                raise ValueError("Owner goal authority requires goal creation or explicit resume.")
            # The automatic turn-end pause/block must not overwrite progress
            # written by a separate tool process after ACP's precheck. Check
            # the entire immutable snapshot under the same lock as the write.
            if expected_goal is not None and goal != expected_goal:
                raise ValueError("Goal changed during resume; refresh its state.")
            if goal_id is not None and (goal is None or goal.id != goal_id):
                raise ValueError("This goal was replaced or cleared; refresh its state.")
            if expected_status is not None and (goal is None or goal.status != expected_status):
                raise ValueError(
                    (pause.owner_instruction if (pause := self.goal_pause(name)) else None)
                    or "This goal is no longer active; refresh its state."
                )
            original_goal = goal
            edited_pause = self.goal_pause(name) if action == "edit" else None
            wait_targets: tuple[GoalWaitTarget, ...] = ()
            if action == "standby":
                if goal is None:
                    raise ValueError("No goal is set for this thread.")
                review = self._goal_input_review(thread, goal.id, wait_for)
                wait_targets = review.targets
                from .input_disposition import AcpDeliveryCursors, InputDispositions

                aliases = review.owners
                cursor = AcpDeliveryCursors(self.root).cursor(aliases)
                dispositions = InputDispositions(self.root)
                unknown = {row["key"]: row for row in review.unknown}
                reviewed_keys = tuple(dict.fromkeys(reviewed_inputs))
                if any(
                    key not in unknown or unknown[key]["sequence"] is None for key in reviewed_keys
                ):
                    raise ValueError(
                        "Review only this recipient's exact unresolved bus input keys."
                    )
                reviewed_sequences = {unknown[key]["sequence"] for key in reviewed_keys}
                prior_reviews = {
                    row["sequence"]
                    for row in unknown.values()
                    if goal is not None and dispositions.reviewed_for_goal(row, goal.id)
                }
                unresolved = {
                    row["sequence"] for row in unknown.values() if row["sequence"] is not None
                }
                senders = review.senders
                if not set(reviewed_keys) <= review.eligible_keys:
                    raise ValueError("Review only direct inputs from these declared dependencies.")
                pending = self.bus._history_page(
                    lambda message: message.target in aliases
                    and message.sender in senders
                    and (message.seq > cursor or message.seq in unresolved)
                    and message.seq not in reviewed_sequences | prior_reviews,
                    before=None,
                    after=None,
                    limit=1,
                    max_bytes=256 * 1024,
                )
                if pending.messages:
                    sequence = pending.messages[0].seq
                    raise ValueError(
                        f"Dependency reply {sequence} is already pending or UNKNOWN. "
                        f"Call comms_inbox with goal_id={goal.id!r} and "
                        f"wait_for={list(wait_for)!r}. Inspect standby_review.messages, then "
                        "pass only standby_review.reviewed_inputs to comms_goal to wait "
                        "for a later reply. Do not pass excluded owner or other dependency inputs. "
                        "This does not mark them STARTED or replay them."
                    )
                snapshot = self.registry.snapshot()
                if not any(
                    GoalWaits.target_has_active_turn(target, snapshot)
                    and self._process_alive(
                        snapshot.threads[snapshot.aliases.get(target.name, target.name)].pid
                    )
                    for target in wait_targets
                ):
                    names = ", ".join(f"@{target.name}" for target in wait_targets)
                    raise ValueError(
                        f"No declared dependency has an active turn ({names}). "
                        "A running/ready process or queued input does not prove active work. "
                        "Message or restart the responsible agent, inspect its status, "
                        "then declare standby only while a target is actually working."
                    )
                closed = GoalWaits.closed_wait_group(
                    thread.name,
                    wait_targets,
                    GoalWaits(self.root / "goal_waits.json").snapshot(),
                    snapshot,
                    self._process_alive,
                )
                if closed:
                    names = ", ".join(f"@{name}" for name in closed)
                    raise ValueError(
                        f"Standby would close a dependency wait group ({names}). "
                        "At least one agent must remain able to work or reply. "
                        "Continue independent work or change the dependencies."
                    )
            elif wait_for or reviewed_inputs:
                raise ValueError("wait_for and reviewed_inputs are only valid for standby.")
            report_turn = thread.active_turn.id if thread.active_turn is not None else ""
            if model_report and thread.last_goal_report_turn == report_turn:
                raise ValueError("This goal was already reported in this turn.")
            new_goal = (
                Goal(text=text.strip(), id=uuid4().hex, revision=1) if action == "set" else None
            )
            if new_goal is not None:
                new_goal = replace(
                    new_goal,
                    mention_source=bind_goal_mentions(
                        new_goal.text,
                        new_goal.id,
                        new_goal.revision,
                        thread,
                        self.registry.snapshot(),
                    ),
                )
            if owner_store is not None and new_goal is not None:
                # The private grant exists before the visible active goal. A
                # crash in between leaves only an unreachable ledger row.
                owner_store.create_goal(new_goal.id)
            if action in {"clear", "set"} and goal is not None:
                # Revoke a protected goal before removing or replacing its
                # registry identity. If the registry write then fails, the
                # remaining visible goal is safely unlaunchable.
                from .goal_attempts import GoalAttemptStore

                private = self.root / "goal-private"
                if (private / "goal_attempts.sqlite3").exists():
                    attempts = GoalAttemptStore(private)
                    generation = attempts.snapshot(goal.id)
                    if generation is not None and generation.state not in {
                        "completed",
                        "cancelled",
                    }:
                        attempts.retire_goal(
                            goal.id,
                            expected_generation=generation.number,
                            attempt_id=generation.attempt_id,
                        )
            if action == "set":
                # A replacement has a fresh unpredictable ID; revisions are
                # monotone within that goal's identity, not across goals.
                goal = new_goal
            elif action == "clear":
                goal = None
            elif action == "edit":
                if goal is None:
                    raise ValueError("No goal is set for this thread.")
                if not text.strip():
                    raise ValueError("A goal requires text.")
                edited_text = text.strip()
                edited_revision = goal.revision + 1
                goal = replace(
                    goal,
                    text=edited_text,
                    revision=edited_revision,
                    mention_source=bind_goal_mentions(
                        edited_text,
                        goal.id,
                        edited_revision,
                        thread,
                        self.registry.snapshot(),
                    ),
                )
            elif action in {"active", "standby", "paused", "blocked", "completed"}:
                if action == "blocked":
                    # Do not recycle a prior progress report as the reason.
                    reason = _required_block_reason(
                        block_reason if block_reason is not None else progress
                    )
                elif block_reason is not None:
                    raise ValueError("Only a blocked goal can have a block reason.")
                else:
                    reason = None
                if goal is None:
                    raise ValueError("No goal is set for this thread.")
                if goal.status == "blocked" and action != "blocked":
                    raise ValueError("Blocked goal requires an explicit retry through its owner.")
                if goal.status == "completed" and action != "completed":
                    raise ValueError("A completed goal cannot be resumed; set a new goal.")
                if action == "active" and owner_store is not None:
                    generation = owner_store.snapshot(goal.id)
                    if generation is None:
                        raise ValueError(
                            "Goal launch authority is missing; inspect it before Retry."
                        )
                    if generation.state == "blocked" and generation.attempt_id:
                        # A failed/uncertain attempt needs the explicit Retry
                        # decision, not a status-only Resume. Expose that state
                        # immediately so the UI offers the correct control.
                        # Persist the bounded refusal explanation so a reload
                        # never shows 'reason unavailable' on a fresh row.
                        refusal = _required_block_reason(
                            "The interrupted goal attempt is unresolved. Inspect it, then use "
                            "Retry to authorize a new attempt. Your messages can still be sent."
                        )
                        blocked = replace(
                            goal,
                            status="blocked",
                            progress=goal.progress,
                            block_reason=refusal,
                            revision=goal.revision + 1,
                        )
                        self.registry.register(
                            replace(thread, goal=blocked), self.registry.status(thread.name)
                        )
                        raise ValueError(refusal)
                    elif generation.state == "ready":
                        pass
                    elif not (generation.state == "reserved" and thread.active_turn is not None):
                        raise ValueError("The goal attempt is unresolved; inspect it before Retry.")
                goal = replace(
                    goal,
                    status="active" if action == "standby" else action,
                    progress=goal.progress if progress is None else progress,
                    block_reason=reason,
                    revision=goal.revision + 1,
                    reported_turn=report_turn if model_report else goal.reported_turn,
                )
            else:
                raise ValueError(f"Unknown goal action: {action}")
            waits = GoalWaits(self.root / "goal_waits.json")
            if action == "standby":
                assert goal is not None
                dispositions.review_for_goal(
                    reviewed_keys,
                    owners=aliases,
                    goal_id=goal.id,
                    goal_revision=goal.revision,
                    turn_id=report_turn,
                )
                # Commit scheduling authority first. A crash before the registry
                # progress update must leave this same goal waiting, not runnable.
                waits.record(
                    GoalWait(
                        goal.id,
                        uuid4().hex,
                        goal.revision,
                        self.message_high_water(),
                        wait_targets,
                        owner_created_at=thread.created_at,
                        report_turn_id=thread.active_turn.id if thread.active_turn else None,
                        report_turn_generation=(
                            thread.turn_generation if thread.active_turn else None
                        ),
                        target_turn_generations=tuple(
                            (
                                snapshot.threads[
                                    snapshot.aliases.get(target.name, target.name)
                                ].turn_generation
                                if GoalWaits.target_has_active_turn(target, snapshot)
                                else None
                            )
                            for target in wait_targets
                        ),
                    )
                )
            self.registry.register(
                replace(
                    thread,
                    goal=goal,
                    last_goal_report_turn=(
                        report_turn if model_report else thread.last_goal_report_turn
                    ),
                ),
                self.registry.status(thread.name),
            )
            if action != "standby" and action != "edit" and original_goal is not None:
                waits.clear(original_goal.id)
            if action == "edit" and edited_pause is not None and goal is not None:
                GoalPauseEvents(self.root / "goal_pause_events.json").record(
                    GoalPauseEvent(goal.id, goal.revision, edited_pause.source)
                )
            if action == "paused" and goal is not None:
                # The registry transition precedes attribution. A crash in between
                # leaves an unknown actor, never attributes a later pause falsely.
                source = (
                    GoalPauseSource.OWNER
                    if owner_action
                    else GoalPauseSource.MODEL if model_report else GoalPauseSource.RUNTIME
                )
                GoalPauseEvents(self.root / "goal_pause_events.json").record(
                    GoalPauseEvent(goal.id, goal.revision, source)
                )
            return goal

    def block_goal_after_failed_turn(
        self,
        name: str,
        *,
        started_goal: Goal,
        expected_worktree: str,
        diagnostic: str,
    ) -> Goal | None:
        """Make an unresolved same-ID attempt visible without losing newer progress."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            current = thread.goal
            if (
                thread.worktree != expected_worktree
                or current is None
                or current.id != started_goal.id
                or current.status not in {"active", "paused", "completed"}
            ):
                return current
            if (
                current.status == "paused"
                and (pause := self.goal_pause(name)) is not None
                and pause.source is GoalPauseSource.OWNER
            ):
                # Preserve this exact owner-authored pause. The caller still
                # records the failed private attempt and terminal diagnostic;
                # preserving intent grants neither resume nor replay authority.
                return current
            progress = f"{current.progress}\n\n{diagnostic}" if current.progress else diagnostic
            blocked = replace(
                current,
                status="blocked",
                progress=progress,
                block_reason=_required_block_reason(diagnostic),
                revision=current.revision + 1,
            )
            self.registry.register(replace(thread, goal=blocked), self.registry.status(thread.name))
            return blocked

    def block_unverified_goal_completion(
        self,
        name: str,
        *,
        expected_goal: Goal,
        expected_worktree: str,
        diagnostic: str,
    ) -> Goal | None:
        """Revoke a provisional completion after the provider turn failed."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            current = thread.goal
            if (
                thread.worktree != expected_worktree
                or current is None
                or current != expected_goal
                or current.status != "completed"
            ):
                return current
            blocked = replace(
                current,
                status="blocked",
                progress=diagnostic,
                block_reason=_required_block_reason(diagnostic),
                revision=current.revision + 1,
            )
            self.registry.register(replace(thread, goal=blocked), self.registry.status(thread.name))
            return blocked

    def acquire_thread(self, name: str, *, owner_pid: int) -> Thread:
        """Claim an offline thread, or return its existing live owner unchanged."""
        from dataclasses import replace

        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            if not thread.role.executable:
                raise RelationViolationError("A human participant cannot become an agent executor.")
            if thread.pid > 0 and thread.pid != owner_pid and self._process_alive(thread.pid):
                return thread
            reservation = os.environ.pop("AGENT_COMMS_RESERVATION_FD", None)
            if reservation is not None:
                try:
                    fd = int(reservation)
                    ready, _, _ = select.select([fd], [], [], 5.0)
                    evidence = os.read(fd, hashlib.sha256().digest_size) if ready else b""
                except (OSError, ValueError) as error:
                    raise RelationViolationError("Invalid owner startup reservation.") from error
                finally:
                    with suppress(OSError, ValueError):
                        os.close(int(reservation))
                epoch = self.registry.snapshot().owner_epochs.get(thread.name)
                if epoch is None:
                    raise RelationViolationError("Owner startup reservation has no incarnation.")
                expected = _owner_launch_proof(thread.name, owner_pid, epoch)
                if (
                    evidence != expected
                    or thread.pid != owner_pid
                    or thread.active_turn is not None
                    or not self.registry.status(thread.name).active
                ):
                    raise RelationViolationError("Owner startup reservation no longer matches.")
                # An inherited pipe from our own launcher proves this exact
                # registry reservation. A coincidentally reused PID cannot.
                return thread
            owned = replace(thread, pid=owner_pid, active_turn=None)
            self.registry.register(owned, new_owner=True)
            return owned

    def ensure_owner(
        self, name: str, *, agent_bin: str = "pi", agent_args: Sequence[str] | None = None
    ) -> Thread:
        """Attach or launch an active thread; never revive an intentionally stopped one."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            if not thread.role.executable:
                raise RelationViolationError("A human participant cannot become an agent executor.")
            if not self.registry.status(thread.name).active:
                raise RelationViolationError(
                    f"Thread {thread.name!r} is stopped or archived; use explicit comms_start."
                )
            if thread.pid > 0 and self._process_alive(thread.pid):
                return thread
            return self._launch_owner_unlocked(thread, agent_bin, agent_args)

    def start(
        self, name: str, *, agent_bin: str | None = None, agent_args: Sequence[str] | None = None
    ) -> OwnerStartResult:
        """Explicitly resume a visible agent thread, reserving at most one owner.

        Starting an already-live thread is idempotent. This does not submit a
        prompt or interrupt a turn; the detached owner resumes its saved state.
        The PID receipt is a reservation, not a completed startup handshake.
        """
        original_owner: tuple[str, int, float] | None = None
        original_epoch: int | None = None
        for _ in range(3):
            with _store_lock(self._wire_lock_path):
                snapshot = self.registry.snapshot()
                canonical = snapshot.aliases.get(name, name)
                thread = self.registry.require(name)
                if not thread.role.executable or not snapshot.statuses[canonical].visible:
                    raise RelationViolationError("Only visible agent threads can be started.")
                identity = (canonical, thread.pid, thread.created_at)
                if original_owner is not None and identity != original_owner:
                    raise RelationViolationError(f"Owner changed while starting {name!r}.")
                original_owner = identity
                epoch = snapshot.admission_generations.get(canonical)
                if original_epoch is not None and epoch != original_epoch:
                    raise RelationViolationError(f"Owner epoch changed while starting {name!r}.")
                if thread.pid <= 0 or not self._process_alive(thread.pid):
                    owner = self._launch_owner_unlocked(
                        thread,
                        agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", "pi"),
                        agent_args,
                    )
                    return OwnerStartResult(owner.name, owner.pid, True)
                if epoch is None:
                    raise RelationViolationError("Cannot start an owner without an incarnation.")
                original_epoch = epoch

            # A just-forked worker needs the wire lock to create its socket.
            if not self._is_local_participant(thread):
                raise RelationViolationError(
                    f"Cannot reuse unverifiable process {thread.pid} for {thread.name!r}."
                )
            with _store_lock(self._wire_lock_path):
                current = self.registry.snapshot()
                fresh = current.threads.get(canonical)
                if (
                    fresh is None
                    or (canonical, fresh.pid, fresh.created_at) != original_owner
                    or not current.statuses.get(canonical, ThreadStatus.STOPPED).visible
                ):
                    raise RelationViolationError(f"Owner changed while starting {name!r}.")
                if current.admission_generations.get(canonical) != original_epoch:
                    raise RelationViolationError(f"Owner epoch changed while starting {name!r}.")
                if not self._process_alive(fresh.pid) or not self._is_local_participant(
                    fresh, wait=False
                ):
                    continue
                if not current.statuses[canonical].active:
                    self.registry.register(fresh)
                return OwnerStartResult(canonical, fresh.pid, False)
        raise RelationViolationError(
            f"Owner changed or became unverifiable while starting {name!r}."
        )

    def restart_owners(
        self,
        names: Sequence[str] | None = None,
        *,
        agent_bin: str = "pi",
        agent_args: Sequence[str] | None = None,
    ) -> tuple[OwnerRestartResult, ...]:
        """Preflight all owners together; release the wire lock for proof and exit.

        No owner is signaled until every selected owner has been verified again
        under the wire lock. The OS may still fail partway through signaling;
        that uncertainty is reported rather than claiming an atomic restart.
        """
        selection: tuple[tuple[str, int, float], ...] | None = None
        original_epochs: tuple[int, ...] | None = None
        for _ in range(3):
            with _store_lock(self._wire_lock_path):
                snapshot = self.registry.snapshot()
                if names is None:
                    threads = [
                        thread
                        for thread in snapshot.threads.values()
                        if thread.role.executable
                        and snapshot.statuses[thread.name].active
                        and thread.pid > 0
                        and self._process_alive(thread.pid)
                    ]
                else:
                    threads = list(
                        {
                            snapshot.aliases.get(name, name): self.registry.require(name)
                            for name in names
                        }.values()
                    )
                identities = tuple(
                    (thread.name, thread.pid, thread.created_at) for thread in threads
                )
                if selection is not None and identities != selection:
                    raise RelationViolationError("Owner selection changed before restart.")
                selection = identities
                captured = []
                for thread in threads:
                    if (
                        not thread.role.executable
                        or not snapshot.statuses[thread.name].active
                        or thread.pid <= 0
                        or not self._process_alive(thread.pid)
                    ):
                        raise ValueError(f"Thread {thread.name!r} has no running owner to restart.")
                    if thread.pid == os.getpid():
                        raise ValueError("An owner cannot restart itself; use the external CLI.")
                    if thread.active_turn is not None:
                        raise ValueError(
                            f"Thread {thread.name!r} has an active turn; wait until idle."
                        )
                    epoch = snapshot.admission_generations.get(thread.name)
                    if epoch is None:
                        raise RelationViolationError(
                            "Cannot restart an owner without an incarnation."
                        )
                    captured.append((thread, epoch))
                epochs = tuple(epoch for _thread, epoch in captured)
                if original_epochs is not None and epochs != original_epochs:
                    raise RelationViolationError("Owner epochs changed before restart.")
                original_epochs = epochs

            # A newly forked owner's socket may depend on this same wire lock.
            for thread, _epoch in captured:
                if not self._is_local_participant(thread):
                    raise RelationViolationError(
                        f"Refusing to restart unverifiable process {thread.pid} "
                        f"for {thread.name!r}."
                    )
            with _store_lock(self._wire_lock_path):
                fresh = self.registry.snapshot()
                if any(
                    (
                        fresh.threads.get(thread.name) is None
                        or (
                            thread.name,
                            fresh.threads[thread.name].pid,
                            fresh.threads[thread.name].created_at,
                        )
                        != (thread.name, thread.pid, thread.created_at)
                        or not fresh.statuses.get(thread.name, ThreadStatus.STOPPED).active
                    )
                    for thread, _epoch in captured
                ):
                    raise RelationViolationError("Owner selection changed before restart.")
                if any(
                    fresh.admission_generations.get(thread.name) != epoch
                    for thread, epoch in captured
                ):
                    raise RelationViolationError("Owner epochs changed before restart.")
                if any(
                    fresh.threads[thread.name].active_turn is not None
                    for thread, _epoch in captured
                ):
                    raise RelationViolationError("Owner became busy before restart.")
                if any(
                    not self._process_alive(thread.pid)
                    or not self._is_local_participant(thread, wait=False)
                    for thread, _epoch in captured
                ):
                    continue
                for thread, _epoch in captured:
                    with suppress(ProcessLookupError):
                        self._signal_local_owner(thread.pid, signal.SIGTERM)
                break
        else:
            raise RelationViolationError("Owner selection changed or became unverifiable.")

        alive = [
            (thread, epoch)
            for thread, epoch in captured
            if not self._wait_for_owner_exit(thread.pid, 3.0)
        ]
        if alive:
            with _store_lock(self._wire_lock_path):
                for thread, epoch in alive:
                    self._require_same_stop_owner(thread, epoch)
                    if not self._is_local_participant(thread, wait=False):
                        raise RelationViolationError(
                            f"Refusing to signal unverifiable process {thread.pid}."
                        )
                for thread, _epoch in alive:
                    with suppress(ProcessLookupError):
                        self._signal_local_owner(thread.pid, signal.SIGKILL)
            remaining = [
                thread.pid
                for thread, _epoch in alive
                if not self._wait_for_owner_exit(thread.pid, 1.0)
            ]
            if remaining:
                diagnostics = "; ".join(self._stop_failure_probe(pid) for pid in remaining)
                raise RuntimeError(f"Owner processes did not stop: {diagnostics}")

        with _store_lock(self._wire_lock_path):
            final = self.registry.snapshot()
            for thread, epoch in captured:
                current = final.threads.get(thread.name)
                if self._released_same_owner(final, thread, epoch):
                    continue
                self._require_same_stop_owner(thread, epoch)
            for thread, _epoch in captured:
                if self.registry.status(thread.name).active:
                    self.registry.unregister(thread.name)
            results = []
            for thread, _epoch in captured:
                current = self.registry.require(thread.name)
                owner = self._launch_owner_unlocked(current, agent_bin, agent_args)
                results.append(OwnerRestartResult(thread.name, thread.pid, owner.pid))
            return tuple(results)

    def _launch_owner_unlocked(
        self,
        thread: Thread,
        agent_bin: str,
        agent_args: Sequence[str] | None = None,
        *,
        prompt: str | None = None,
    ) -> Thread:
        env = os.environ.copy()
        for key in ("PI_PROMPT", "PI_PARENT_ID", "PI_TASK", "AGENT_COMMS_RESERVATION_FD"):
            env.pop(key, None)
        env.update(
            {
                "PI_AGENT_ID": thread.name,
                "AGENT_COMMS_THREAD": thread.name,
                "PI_AGENT_TAGS": ",".join(sorted(thread.tags)),
                "AGENT_COMMS_TAGS": ",".join(sorted(thread.tags)),
                "PI_WORKTREE": thread.worktree,
                "AGENT_COMMS_ROOT": str(self.root.resolve()),
                "AGENT_COMMS_AGENT_BIN": agent_bin,
            }
        )
        if thread.parent is not None:
            env["PI_PARENT_ID"] = thread.parent
        if thread.task is not None:
            env["PI_TASK"] = thread.task
        if prompt is not None:
            env["PI_PROMPT"] = prompt
        if agent_args is not None:
            env["AGENT_COMMS_AGENT_ARGS"] = shlex.join(agent_args)
        # The read end is inherited only by this worker. Send its committed
        # epoch AFTER registration: a crash before that point fails startup
        # closed instead of making a stale same-PID record authoritative.
        read_fd, write_fd = os.pipe() if os.name == "posix" else (-1, -1)
        if read_fd >= 0:
            env["AGENT_COMMS_RESERVATION_FD"] = str(read_fd)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "agent_comms.worker"],
                env=env,
                cwd=thread.worktree,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(read_fd,) if read_fd >= 0 else (),
            )
        except BaseException:
            if write_fd >= 0:
                os.close(write_fd)
            raise
        finally:
            if read_fd >= 0:
                os.close(read_fd)
        owned = replace(thread, pid=process.pid, active_turn=None)
        try:
            self.registry.register(owned, new_owner=True)
            if write_fd >= 0:
                epoch = self.registry.snapshot().owner_epochs[thread.name]
                proof = _owner_launch_proof(thread.name, process.pid, epoch)
                if os.write(write_fd, proof) != len(proof):
                    raise RelationViolationError("Owner startup reservation was not fully sent.")
        except BaseException:
            process.terminate()
            process.wait()
            raise
        finally:
            if write_fd >= 0:
                os.close(write_fd)
        return owned

    def attach_session(self, name: str, session_file: str, *, pid: int | None = None) -> Thread:
        """Attach authoritative Pi runtime state to an existing thread."""
        with _store_lock(self._wire_lock_path):
            current = self.registry.require(name)
            attached = replace(
                current,
                pid=current.pid if pid is None else pid,
                session_file=str(Path(session_file).expanduser().resolve()),
            )
            self.registry.register(attached)
            return attached

    def stop(self, name: str) -> None:
        """Stop one verified owner without holding the wire lock through startup or exit.

        On Darwin the worker must acquire this lock before it can create the
        kernel-authenticated owner socket. Waiting for that socket under the
        lock would prevent a newly forked worker from ever proving its PID.
        """
        original_owner: tuple[str, int, float] | None = None
        original_epoch: int | None = None
        for attempt in range(3):
            with _store_lock(self._wire_lock_path):
                snapshot = self.registry.snapshot()
                canonical = snapshot.aliases.get(name, name)
                thread = snapshot.threads.get(canonical)
                if thread is None:
                    self.registry.require(name)  # Preserve the ordinary unknown-name error.
                    raise AssertionError("Registered thread disappeared from its snapshot")
                identity = (canonical, thread.pid, thread.created_at)
                if original_owner is not None and identity != original_owner:
                    raise RelationViolationError(
                        f"Owner changed while stopping {name!r}; refusing a stale signal."
                    )
                original_owner = identity
                epoch = snapshot.admission_generations.get(canonical)
                if original_epoch is not None and epoch != original_epoch:
                    raise RelationViolationError(f"Owner epoch changed while stopping {name!r}.")
                if not snapshot.statuses[canonical].active:
                    if attempt == 0:
                        return
                    raise RelationViolationError(f"Owner changed while stopping {name!r}.")
                if thread.pid == os.getpid():
                    caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
                    if caller and self.registry.require(caller).name == canonical:
                        self._release_current_owner_unlocked(canonical)
                    else:
                        self.registry.unregister(canonical)
                    return
                if thread.pid <= 0 or not self._process_alive(thread.pid):
                    self.registry.unregister(canonical)
                    return
                if epoch is None:
                    raise RelationViolationError("Cannot stop an owner without an incarnation.")
                original_epoch = epoch

            # Do not wait while holding the wire lock: startup and graceful
            # shutdown both need it. Recheck the exact incarnation before any
            # signal, so a replaced owner cannot inherit an old stop request.
            if not self._is_local_participant(thread):
                raise RelationViolationError(
                    f"Refusing to signal unverifiable process {thread.pid} for {name!r}."
                )
            with _store_lock(self._wire_lock_path):
                current = self.registry.snapshot()
                current_thread = current.threads.get(canonical)
                if (
                    current_thread is None
                    or (canonical, current_thread.pid, current_thread.created_at) != original_owner
                    or not current.statuses.get(canonical, ThreadStatus.STOPPED).active
                ):
                    raise RelationViolationError(
                        f"Owner changed while stopping {name!r}; refusing a stale signal."
                    )
                if current.admission_generations.get(canonical) != original_epoch:
                    raise RelationViolationError(f"Owner epoch changed while stopping {name!r}.")
                if not self._process_alive(thread.pid):
                    self.registry.unregister(canonical)
                    return
                # Nonblocking fresh kernel proof immediately before signaling.
                if not self._is_local_participant(thread, wait=False):
                    continue
                try:
                    self._signal_local_owner(thread.pid, signal.SIGTERM)
                except ProcessLookupError:
                    self.registry.unregister(canonical)
                    return
                break
        else:
            raise RelationViolationError(
                f"Owner changed or became unverifiable while stopping {name!r}."
            )

        if self._wait_for_owner_exit(thread.pid, 3.0):
            self._finish_stopped_owner(thread, epoch)
            return
        with _store_lock(self._wire_lock_path):
            self._require_same_stop_owner(thread, epoch)
            if not self._is_local_participant(thread, wait=False):
                raise RelationViolationError(
                    f"Refusing to signal unverifiable process {thread.pid} for {name!r}."
                )
            with suppress(ProcessLookupError):
                self._signal_local_owner(thread.pid, signal.SIGKILL)
        if not self._wait_for_owner_exit(thread.pid, 1.0):
            raise RuntimeError(f"Process did not stop: {self._stop_failure_probe(thread.pid)}")
        self._finish_stopped_owner(thread, epoch)

    @staticmethod
    def _stop_failure_probe(pid: int) -> str:
        """Bounded state-only Mac CI diagnostic, never a death or ownership proof."""
        if sys.platform != "darwin":
            return f"pid={pid}"
        try:
            probe = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
                timeout=1,
            )
            state = f"ps_rc={probe.returncode}, ps_stat={probe.stdout.strip()[:24]!r}"
        except (OSError, subprocess.TimeoutExpired) as error:
            state = f"ps_error={type(error).__name__}"
        # In a failing stop only, find whether this PID is our exited child.
        # waitpid may reap it; the result is diagnostic, not false success.
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
            child = f"waitpid={reaped}"
        except (ChildProcessError, OSError) as error:
            child = f"waitpid_error={type(error).__name__}"
        return f"pid={pid}, {state}, {child}"

    @staticmethod
    def _signal_local_owner(pid: int, signum: signal.Signals) -> None:
        if os.name == "posix" and os.getpgid(pid) == pid:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)

    def _require_same_stop_owner(self, thread: Thread, epoch: int) -> None:
        snapshot = self.registry.snapshot()
        current = snapshot.threads.get(thread.name)
        if (
            current is None
            or (current.name, current.pid, current.created_at)
            != (thread.name, thread.pid, thread.created_at)
            or snapshot.admission_generations.get(thread.name) != epoch
            or not snapshot.statuses.get(thread.name, ThreadStatus.STOPPED).active
        ):
            raise RelationViolationError(
                f"Owner changed while stopping {thread.name!r}; refusing a stale signal."
            )

    def _wait_for_owner_exit(self, pid: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            # Observe a direct child's exit WITHOUT reaping it behind its
            # Popen/parent's back. An unrelated CLI process has no waitid
            # authority and falls back to ps/proc.
            if os.name == "posix" and hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
                try:
                    result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if result is not None and result.si_pid == pid:
                        return True
                except (ChildProcessError, OSError):
                    pass
            if not self._process_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _read_owner_release_receipts(self) -> dict[str, dict[str, object]]:
        try:
            raw = json.loads((self.root / "owner_release_receipts.json").read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise RelationViolationError("Owner release receipt is unreadable.") from error
        if not isinstance(raw, dict) or any(not isinstance(value, dict) for value in raw.values()):
            raise RelationViolationError("Owner release receipt is invalid.")
        return raw

    def _released_same_owner(self, snapshot: RegistrySnapshot, thread: Thread, epoch: int) -> bool:
        current = snapshot.threads.get(thread.name)
        if (
            snapshot.statuses.get(thread.name) is not ThreadStatus.STOPPED
            or current is None
            or current.active_turn is not None
            or (current.name, current.pid, current.created_at)
            != (thread.name, thread.pid, thread.created_at)
        ):
            return False
        return self._read_owner_release_receipts().get(thread.name) == {
            "pid": thread.pid,
            "before": epoch,
            "after": snapshot.admission_generations.get(thread.name),
            "thread": json.dumps(current.to_wire(), sort_keys=True),
        }

    def _finish_stopped_owner(self, thread: Thread, epoch: int) -> None:
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            status = snapshot.statuses.get(thread.name)
            if status is ThreadStatus.STOPPED and self._released_same_owner(
                snapshot, thread, epoch
            ):
                return  # An exact, durably attested release of the signaled owner.
            self._require_same_stop_owner(thread, epoch)
            self.registry.unregister(thread.name)

    def release(self, name: str) -> None:
        """Let the calling participant mark itself stopped without signalling."""
        caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
        if not caller:
            raise RelationViolationError(
                "Voluntary release requires PI_AGENT_ID or AGENT_COMMS_THREAD."
            )
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(name).name
            if self.registry.require(caller).name != canonical:
                raise RelationViolationError(f"Thread {caller!r} cannot release {canonical!r}.")
            self._release_current_owner_unlocked(canonical)

    def _release_current_owner_unlocked(self, canonical: str) -> None:
        snapshot = self.registry.snapshot()
        owner = snapshot.threads[canonical]
        if owner.pid > 0 and owner.pid != os.getpid():
            raise RelationViolationError("Only the registered owner may release itself.")
        before = snapshot.admission_generations[canonical]
        self.registry.unregister(canonical)
        after = self.registry.snapshot().admission_generations[canonical]
        receipts = self._read_owner_release_receipts()
        receipts[canonical] = {
            "pid": owner.pid,
            "before": before,
            "after": after,
            "thread": json.dumps(replace(owner, active_turn=None).to_wire(), sort_keys=True),
        }
        _atomic_write_text(
            self.root / "owner_release_receipts.json",
            json.dumps(receipts, sort_keys=True),
            fsync_parent=True,
        )

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if sys.platform == "win32":
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # Access denied still means it exists.
            try:
                code = ctypes.c_uint()
                return (
                    bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    and code.value == 259
                )
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # An inaccessible PID is not proof of death. Never unregister a
            # possibly live owner merely because its liveness probe failed.
            return True
        if sys.platform.startswith("linux"):
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().split()
            except FileNotFoundError:
                return False
            except OSError:
                return True
            return not (len(stat) > 2 and stat[2] == "Z")
        try:
            # A caller may deliberately clear PATH (including CLI subprocesses).
            # Do not mistake failure to locate ps for a dead macOS owner.
            probe = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return True
        if probe.returncode != 0:
            return True  # Unknown, not an authoritative dead-process receipt.
        return not probe.stdout.strip().startswith("Z")

    def _is_local_participant(self, thread: Thread, *, wait: bool = True) -> bool:
        """Prove a PID belongs to the named participant before signaling it."""
        if sys.platform == "darwin":
            import socket

            from .runtime import socket_path

            deadline = time.monotonic() + (2 if wait else 0)
            while True:
                try:
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.settimeout(0.5)
                        connection.connect(str(socket_path(self.root, thread.pid)))
                        # SOL_LOCAL / LOCAL_PEERPID: kernel-authenticated owner PID.
                        return bool(connection.getsockopt(0, 2) == thread.pid)
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.05)
                except OSError:
                    return False
        if not sys.platform.startswith("linux"):
            return False
        try:
            entries = Path(f"/proc/{thread.pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return False
        environ = dict(item.split(b"=", 1) for item in entries if item and b"=" in item)
        expected = {name.encode() for name in self.registry.aliases_for(thread.name)}
        name_matches = any(
            item.startswith((b"AGENT_COMMS_THREAD=", b"PI_AGENT_ID="))
            and item.split(b"=", 1)[1] in expected
            for item in entries
        )
        root = Path(environ.get(b"AGENT_COMMS_ROOT", b"~/.agent-comms").decode()).expanduser()
        if root.resolve() != self.root.resolve():
            return False
        if name_matches:
            return True
        # ACP owners created before their generated identity was known prove
        # ownership via their wire-scoped Unix socket and kernel credentials.
        import socket
        import struct

        from .runtime import socket_path

        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(0.5)
                connection.connect(str(socket_path(self.root, thread.pid)))
                pid, uid, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                return bool(pid == thread.pid and uid == os.getuid())
        except OSError:
            return False

    def archive(self, name: str) -> None:
        """Hide a stopped participant from presence while retaining messages."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(name).name
            if not self.registry.status(canonical).stopped:
                raise RelationViolationError("Stop a running thread before archiving it.")
            self.registry.archive(canonical)
            self.runtime_info.remove(canonical)

    def delete(self, name: str) -> DeleteThreadResult:
        """Remove a stopped thread and its owned state, preserving its children."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(name).name
            if not self.registry.status(canonical).stopped:
                raise RelationViolationError("Stop a running thread before deleting it.")
            self.bus.assert_legacy_rewrite_allowed()
            self.registry.begin_delete(canonical)
            messages_removed, markers_removed = self.bus.remove_thread(canonical)
            activity_removed = self.activity.remove_thread(canonical)
            runtime_removed = self.runtime_info.get(canonical) is not None
            self.runtime_info.remove(canonical)
            ledger_removed = self.ledger.remove_thread(canonical)
            self.channel_catalog.remove_thread(canonical)
            detached_children = self.registry.remove(canonical)
            return DeleteThreadResult(
                name=canonical,
                messages_removed=messages_removed,
                markers_removed=markers_removed,
                activity_events_removed=activity_removed,
                runtime_removed=runtime_removed,
                ledger_references_removed=ledger_removed,
                detached_children=detached_children,
            )

    # ─── Forking ──────────────────────────────────────────────────────────────

    def fork(self, spec: ForkSpec, pi_bin: str | None = None) -> Thread:
        """Fork with the current owner's backend executable unless overridden."""
        resolved_bin = pi_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", "pi")
        with _store_lock(self._wire_lock_path):
            return self._fork_unlocked(spec, resolved_bin)

    def _fork_unlocked(self, spec: ForkSpec, pi_bin: str) -> Thread:
        """Spawn a child pi thread from the parent's session.

        Proves the parent is registered and has a session file, declares the
        child thread, registers it, then launches the subprocess. Fail-closed:
        if the launch fails the registration is rolled back.
        """
        parent = self.registry.require(spec.parent)
        if self.registry.name_reserved(spec.name):
            raise RelationViolationError(
                f"Thread {spec.name!r} already exists; reuse it instead of forking it again."
            )
        if not parent.session_file:
            raise RelationViolationError(
                f"Parent thread {spec.parent!r} has no session file to fork."
            )

        child = Thread(
            name=spec.name,
            tags=spec.tags,
            worktree=parent.worktree,
            parent=spec.parent,
            task=spec.task,
            pid=0,
            model=parent.model,
            thinking_level=parent.thinking_level,
        )
        self._require_available_new_tags(child.tags)
        self.registry.register(child)
        self.bus.mark_delivered_through(child.name, self.bus.latest_sequence())

        # Non-interactive pi refuses to run without an explicit model, so pass
        # the parent's last model choice through to the child.
        model = (
            tuple(parent.model.split("/", 1))
            if parent.model and "/" in parent.model
            else _session_model(Path(parent.session_file))
        )
        args = ["--print", "--provider", model[0], "--model", model[1]] if model else None
        try:
            return self._launch_owner_unlocked(
                child,
                pi_bin,
                args,
                prompt=spec.prompt or spec.task,
            )
        except OSError:
            self.registry.remove(spec.name)
            raise

    # ─── Ledger ───────────────────────────────────────────────────────────────

    def ledger_read(self) -> Mapping[str, object]:
        return self.ledger.read()

    def ledger_merge(self, updates: Mapping[str, object], author: str) -> None:
        with _store_lock(self._wire_lock_path):
            if author not in self.registry:
                raise UnregisteredThreadError(f"Author {author!r} is not registered.")
            self.ledger.merge(updates, author)

    # ─── Runtime ──────────────────────────────────────────────────────────────

    def adopt_current(self) -> Thread:
        """Declare and register the current process's thread from env."""
        thread = current_thread()
        self.register(thread)
        return thread

    def poll(self, name: str | None = None) -> Mapping:
        """One-shot status snapshot: self state plus inbox."""
        if name is None:
            me = current_thread()
            name = me.name
        self.registry.require(name)
        return {
            "thread": self.thread_detail(name),
            "inbox": [m.to_wire() for m in self.inbox(name)],
            "ledger": self.ledger_read(),
            "peers": [person for person in self.presence() if person["name"] != name],
        }


def wire(root: Path | str | None = None) -> Comms:
    """Build a Comms wire. Defaults to ~/.agent-comms or $AGENT_COMMS_ROOT."""
    if root is None:
        root = os.environ.get("AGENT_COMMS_ROOT", "~/.agent-comms")
    return Comms(Path(root).expanduser())
