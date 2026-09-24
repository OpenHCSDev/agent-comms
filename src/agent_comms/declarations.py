"""OpenHCS agent communications — declaration-owned orchestration.

Each type owns exactly one concept's semantics. Instantiating a type declares
the concept. Required relations are proved at construction time. Unknown
references raise — the system is fail-closed.

The types below (Thread, Message) are the authorities: their __post_init__
proves required relations and normalises values. The stores (ThreadRegistry,
MessageBus, SharedLedger) persist and route; they do not own semantics.

ACP integration: this module sits behind an ACP (Agent Client Protocol)
server. Toad, Zed, or VS Code connect as ACP clients; this layer spawns and
routes between agent subprocesses. See agentclientprotocol.com.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
import tempfile
import time
import uuid
from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum, StrEnum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Self

from .bus_publication import (
    PRIVATE_WIRE_FIELD,
    CommittedInitial,
    has_private_wire_fields,
    initial_sideband,
    public_envelope_digest,
    stable_thread_lookup,
    unique_wire_object,
    validate_initial_record,
)

if TYPE_CHECKING:
    from .coordination import PublicationIntent
    from .private_registry_guard import PrivateRegistryGuard
from .envelope_claim_transitions import (
    ClaimProjection,
    ClaimRelease,
    ClaimTransition,
    apply_transition,
    normalize_existing_file,
    parse_complete_transition_line,
)
from .mentions import MentionCandidate, ThreadMention


class UnregisteredThreadError(ValueError):
    """A reference does not resolve to a registered thread."""


class RelationViolationError(ValueError):
    """A required relation between declarations cannot be proved."""


class ClaimEnvelopeUnknownError(RelationViolationError):
    """A claim row may be visible after failed durability; never replay automatically."""


def _claim_transition_wire(transition: ClaimTransition) -> dict[str, object]:
    return {
        "owner": transition.owner,
        "incarnation": transition.incarnation,
        "seq": transition.seq,
        "message_id": transition.message_id,
        "claims": list(transition.claims),
        "releases": [asdict(release) for release in transition.releases],
        "generation": transition.generation,
    }


def _claim_transition_from_wire(value: object) -> ClaimTransition:
    if type(value) is not dict:
        raise RelationViolationError("Claim transition is not a typed object.")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        return parse_complete_transition_line(encoded)
    except (TypeError, ValueError) as error:
        raise RelationViolationError("Claim transition is malformed.") from error


def _release_resource(worktree: Path, path: str | Path) -> str:
    """Identify a prior canonical claim even if its original file was deleted."""
    physical_root = worktree.resolve(strict=True)
    raw = path if isinstance(path, str) else str(path)
    supplied = Path(raw)
    if str(supplied) != raw:
        raise RelationViolationError("Claim release requires canonical spelling.")
    candidate = supplied if supplied.is_absolute() else physical_root / supplied
    if ".." in candidate.parts:
        raise RelationViolationError("Claim release cannot traverse parent directories.")
    try:
        candidate.relative_to(physical_root)
    except ValueError as error:
        raise RelationViolationError("Claim release is outside the owner's worktree.") from error
    return str(candidate)


def _claim_gate_path(bus_path: Path) -> Path:
    """The EXISTING private bus protocol marker; no second ownership file."""
    return bus_path.with_name("bus_meta.json")


def _claim_gate_enabled(bus_path: Path) -> bool:
    # _store_lock is also used for registry, channels, and marker files.
    # Only the canonical bus may enter this read/durability barrier.
    if bus_path.name != "bus.jsonl":
        return False
    marker = _claim_gate_path(bus_path)
    try:
        metadata = json.loads(marker.read_text(), object_pairs_hook=unique_wire_object)
    except FileNotFoundError:
        return False
    except (ValueError, UnicodeError) as error:
        raise RelationViolationError("Bus protocol marker is malformed.") from error
    if type(metadata) is not dict:
        raise RelationViolationError("Bus protocol marker is not an object.")
    version = metadata.get("claim_envelopes_version")
    if version is None:
        return False
    if type(version) is not int or version != 1:
        raise RelationViolationError("Unsupported claim-envelope protocol marker.")
    return True


def _verify_claim_bus_before_read_unlocked(bus_path: Path) -> None:
    """Make every visible opt-in bus row durable before ANY bus-lock reader sees it.

    The marker is fsynced before the first claim send. A failed bus append may
    leave a complete, visible row: another cooperating process must fsync the
    opened inode and its directory, then reject incomplete/corrupt rows, before
    treating either the announcement or the claim as committed. This hook is
    entered by the shared bus lock, including ordinary inbox/history readers.
    """
    if not _claim_gate_enabled(bus_path):
        return
    marker = _claim_gate_path(bus_path)
    marker_info = marker.lstat()
    if (
        os.name != "posix"
        or not stat.S_ISREG(marker_info.st_mode)
        or marker_info.st_uid != os.geteuid()
        or stat.S_IMODE(marker_info.st_mode) != 0o600
    ):
        raise RelationViolationError("Claim bus read barrier is not durable and private.")
    try:
        # The private marker's source of truth is its single fsynced JSON file.
        # The root identity/sequence/claim flag are validated again by private
        # writers; this preflight protects all ordinary readers on marked roots.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(bus_path, flags)
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RelationViolationError("Claim bus is not a regular file.")
                os.fsync(stream.fileno())
                while line := stream.readline(8 * 1024 * 1024 + 1):
                    if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
                        raise RelationViolationError("Incomplete or oversized claim bus row.")
                    try:
                        row = json.loads(line, object_pairs_hook=unique_wire_object)
                    except (ValueError, UnicodeError) as error:
                        raise RelationViolationError("Malformed claim bus row.") from error
                    if not isinstance(row, dict):
                        raise RelationViolationError("Claim bus row must be an object.")
                    if "claim_transition" in row:
                        try:
                            public = Message.from_wire(row).to_wire()
                        except (KeyError, TypeError, ValueError, AttributeError) as error:
                            raise RelationViolationError("Malformed claim envelope.") from error
                        if {
                            key: value for key, value in row.items() if key != PRIVATE_WIRE_FIELD
                        } != public:
                            raise RelationViolationError("Noncanonical claim envelope.")
        directory_fd = os.open(bus_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise RelationViolationError("Claim bus durability is UNKNOWN.") from error


@contextmanager
def _store_lock(store_path: Path) -> Iterator[None]:
    """Hold an exclusive process lock associated with a wire store."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_path.with_name(f".{store_path.name}.lock")
    with open(lock_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(  # type: ignore[attr-defined]
                        lock_file.fileno(), msvcrt.LK_NBLCK, 1  # type: ignore[attr-defined]
                    )
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _verify_claim_bus_before_read_unlocked(store_path)
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, text: str, *, fsync_parent: bool = False) -> None:
    """Replace a snapshot; private guarded writes also durably sync its name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        # Windows does not expose directory fsync; Linux-only private claim
        # opt-in still requires the full parent-durability boundary below.
        if fsync_parent and os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def file_revision(path: Path) -> tuple[int, int, int, int] | None:
    """Identity of an on-disk revision, including atomic replacements."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _repair_trailing_jsonl(path: Path) -> None:
    """Complete a valid unterminated record or quarantine a truncated one."""
    try:
        with path.open("rb") as stream:
            if not stream.seek(0, os.SEEK_END):
                return
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) == b"\n":
                return
            stream.seek(0)
            data = stream.read()
    except FileNotFoundError:
        return
    boundary = data.rfind(b"\n") + 1
    tail = data[boundary:]
    try:
        json.loads(tail)
    except (json.JSONDecodeError, UnicodeDecodeError):
        corrupt_path = path.with_name(f"{path.name}.corrupt")
        with open(corrupt_path, "ab") as corrupt:
            corrupt.write(tail + b"\n")
            corrupt.flush()
            os.fsync(corrupt.fileno())
        with open(path, "r+b") as output:
            output.truncate(boundary)
            output.flush()
            os.fsync(output.fileno())
    else:
        with open(path, "ab") as output:
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())


def _iter_jsonl_stream(
    records: BinaryIO, *, boundary: int | None = None, label: str = "wire"
) -> Iterator[tuple[Mapping, int]]:
    """One raw-record parser for locked logs and fixed opened-inode snapshots."""
    while boundary is None or records.tell() < boundary:
        raw_line = (
            records.readline() if boundary is None else records.readline(boundary - records.tell())
        )
        if not raw_line:
            break
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if not raw_line.endswith((b"\n", b"\r")):
                break
            raise
        if not isinstance(record, Mapping):
            if label == "wire snapshot":
                raise ValueError("Wire snapshot JSONL record must be an object.")
            raise ValueError(f"JSONL record in {label} must be an object.")
        yield record, len(raw_line)


def _iter_jsonl_records(path: Path) -> Iterator[tuple[Mapping, int]]:
    """Yield JSONL records and encoded sizes without materializing the log."""
    if not path.exists():
        return
    with open(path, "rb") as records:
        yield from _iter_jsonl_stream(records, label=str(path))


def _jsonl_records(path: Path) -> list[Mapping]:
    """Read complete JSONL records, tolerating only a truncated final record."""
    return [record for record, _ in _iter_jsonl_records(path)]


def _append_jsonl(path: Path, record: Mapping) -> None:
    """Append one durable record. Caller must hold the store lock."""
    _repair_trailing_jsonl(path)
    with open(path, "ab") as output:
        output.write(json.dumps(record).encode() + b"\n")
        output.flush()
        os.fsync(output.fileno())


class ThreadStatus(Enum):
    RUNNING = "running"
    IDLE = "idle"
    STOPPED = "stopped"
    ARCHIVED = "archived"
    DELETING = "deleting"

    @property
    def visible(self) -> bool:
        return self not in {self.ARCHIVED, self.DELETING}

    @property
    def active(self) -> bool:
        return self in {self.RUNNING, self.IDLE}

    @property
    def running(self) -> bool:
        return self is self.RUNNING

    @property
    def stopped(self) -> bool:
        return self is self.STOPPED

    @property
    def mutable(self) -> bool:
        return self is not self.DELETING

    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        """Whether an executable thread belongs to the requested viewer projection."""
        return (
            self.active
            or (self is self.STOPPED and show_stopped)
            or (self is self.ARCHIVED and show_archived)
        )

    def presentation(self, title: str, activity: Activity) -> ThreadPresentation:
        if self.active:
            return activity.state.presentation(title, activity.detail)
        return ThreadPresentation(title, "○", self.value.title())


class ActivityState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"

    @property
    def busy(self) -> bool:
        return self is not self.IDLE

    def presentation(self, title: str, detail: str) -> ThreadPresentation:
        if not self.busy:
            return ThreadPresentation(title, "✓", "Ready")
        summary = self.value.title()
        if detail := " ".join(detail.split()):
            summary += f" · {detail}"
        return ThreadPresentation(title, "⌛", summary, busy=True)


@dataclass(frozen=True, slots=True)
class Activity:
    """Declares one thread's current activity (what it is doing right now).

    Emitted by participants and agent turns so other clients can show live
    feedback. Appended to ``activity.jsonl``; the latest event per thread is
    the thread's state. Empty detail is valid (plain thinking).
    """

    thread: str
    state: ActivityState
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.thread:
            raise RelationViolationError("Activity thread cannot be empty.")
        if self.detail and not self.state.busy:
            raise RelationViolationError("Idle activity cannot carry a detail.")
        if len(self.detail) > 200:
            raise ValueError("Activity detail cannot exceed 200 characters.")

    def to_wire(self) -> dict:
        return {
            "thread": self.thread,
            "state": self.state.value,
            "detail": self.detail,
            "ts": self.timestamp,
        }

    @classmethod
    def from_wire(cls, data: Mapping) -> Activity:
        return cls(
            thread=data["thread"],
            state=ActivityState(data["state"]),
            detail=data.get("detail", ""),
            timestamp=data.get("ts", 0.0),
        )


class ActivityLog:
    """Persists Activity events as an append-only JSONL log.

    The latest event per thread is its current activity; stale events
    (older than ``stale_after`` seconds) read as idle.
    """

    def __init__(self, store_path: Path, stale_after: float = 120.0):
        self._path = store_path
        self._stale_after = stale_after
        self._revision: tuple | None = None
        self._latest: dict[str, Activity] = {}
        self._complete_latest: dict[str, Activity] = self._latest
        self._offset = 0

    def emit(self, activity: Activity) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            _append_jsonl(self._path, activity.to_wire())

    def current(self, thread: str, *, active: bool = False) -> Activity:
        """Latest activity for one thread; idle when stale or unknown."""
        latest = self._latest_events().get(thread)
        if latest is None:
            return Activity(thread=thread, state=ActivityState.IDLE)
        if not active and time.time() - latest.timestamp > self._stale_after:
            return Activity(thread=thread, state=ActivityState.IDLE, timestamp=latest.timestamp)
        return latest

    def all_current(self, *, active: frozenset[str] = frozenset()) -> dict[str, Activity]:
        """Latest activity per thread (idle included for known threads)."""
        result = self._latest_events()
        now = time.time()
        return {
            thread: (
                activity
                if thread in active or now - activity.timestamp <= self._stale_after
                else Activity(thread=thread, state=ActivityState.IDLE, timestamp=activity.timestamp)
            )
            for thread, activity in result.items()
        }

    def _latest_events(self) -> Mapping[str, Activity]:
        with _store_lock(self._path):
            revision = file_revision(self._path)
            if revision == self._revision:
                return self._latest
            append = (
                revision is not None
                and self._revision is not None
                and revision[0] == self._revision[0]
                and revision[1] > self._revision[1]
            )
            # Publish a new map: other threads may still be iterating the old
            # snapshot after the store lock has been released.
            complete = self._complete_latest.copy() if append else {}
            offset = self._offset if append else 0
            latest = complete
            if revision is not None:
                with self._path.open("rb") as stream:
                    stream.seek(offset)
                    while raw := stream.readline():
                        terminated = raw.endswith((b"\n", b"\r"))
                        if line := raw.strip():
                            try:
                                record = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                if not terminated:
                                    break
                                raise
                            if not isinstance(record, Mapping):
                                raise ValueError(f"JSONL record in {self._path} must be an object.")
                            event = Activity.from_wire(record)
                            if terminated:
                                complete[event.thread] = event
                            else:
                                # Preserve the existing reader's valid-EOF
                                # behavior, but retry this tail on the next append.
                                latest = {**complete, event.thread: event}
                        if terminated:
                            offset = stream.tell()
            self._complete_latest, self._offset = complete, offset
            self._latest, self._revision = latest, revision
            return self._latest

    def _load(self) -> list[Activity]:
        with _store_lock(self._path):
            return [Activity.from_wire(record) for record in _jsonl_records(self._path)]

    def remove_thread(self, thread: str) -> int:
        """Remove all persisted activity for one thread."""
        with _store_lock(self._path):
            records = [dict(record) for record in _jsonl_records(self._path)]
            retained = [record for record in records if record.get("thread") != thread]
            _atomic_write_text(
                self._path,
                "".join(f"{json.dumps(record)}\n" for record in retained),
            )
        return len(records) - len(retained)

    def rename_thread(self, old_name: str, new_name: str) -> None:
        with _store_lock(self._path):
            records = [dict(record) for record in _jsonl_records(self._path)]
            for record in records:
                if record.get("thread") == old_name:
                    record["thread"] = new_name
            _atomic_write_text(
                self._path,
                "".join(f"{json.dumps(record)}\n" for record in records),
            )


@dataclass(frozen=True, slots=True)
class AgentRuntimeInfo:
    """Declares mutable runtime metadata for one registered thread."""

    thread: str
    model: str | None = None
    session_name: str | None = None
    context_used: int | None = None
    context_size: int | None = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.thread:
            raise RelationViolationError("Runtime-info thread cannot be empty.")
        if self.context_used is not None and self.context_used < 0:
            raise ValueError("Context usage cannot be negative.")
        if self.context_size is not None and self.context_size < 0:
            raise ValueError("Context size cannot be negative.")

    @property
    def context_percent(self) -> float | None:
        if self.context_used is None or not self.context_size:
            return None
        return self.context_used / self.context_size * 100

    @property
    def context_label(self) -> str:
        if self.context_percent is None:
            return ""
        return f"{self.context_used:,}/{self.context_size:,} tokens ({self.context_percent:.1f}%)"

    def to_wire(self) -> dict:
        return {
            "thread": self.thread,
            "model": self.model,
            "session_name": self.session_name,
            "context_used": self.context_used,
            "context_size": self.context_size,
            "ts": self.timestamp,
        }

    @classmethod
    def from_wire(cls, data: Mapping) -> AgentRuntimeInfo:
        return cls(
            thread=data["thread"],
            model=data.get("model"),
            session_name=data.get("session_name"),
            context_used=data.get("context_used"),
            context_size=data.get("context_size"),
            timestamp=data.get("ts", 0.0),
        )


class RuntimeInfoStore:
    """Persists the latest runtime metadata for each thread."""

    def __init__(self, store_path: Path):
        self._path = store_path

    def set(self, info: AgentRuntimeInfo) -> None:
        with _store_lock(self._path):
            values = self._load_unlocked()
            values[info.thread] = info
            _atomic_write_text(
                self._path,
                json.dumps({name: value.to_wire() for name, value in values.items()}, indent=2),
            )

    def get(self, thread: str) -> AgentRuntimeInfo | None:
        return self._load().get(thread)

    def all(self) -> Mapping[str, AgentRuntimeInfo]:
        return self._load()

    def remove(self, thread: str) -> None:
        with _store_lock(self._path):
            values = self._load_unlocked()
            values.pop(thread, None)
            _atomic_write_text(
                self._path,
                json.dumps({name: value.to_wire() for name, value in values.items()}, indent=2),
            )

    def rename_thread(self, old_name: str, new_name: str) -> None:
        with _store_lock(self._path):
            values = self._load_unlocked()
            if info := values.pop(old_name, None):
                values[new_name] = AgentRuntimeInfo(
                    thread=new_name,
                    model=info.model,
                    session_name=info.session_name,
                    context_used=info.context_used,
                    context_size=info.context_size,
                    timestamp=info.timestamp,
                )
            _atomic_write_text(
                self._path,
                json.dumps({name: value.to_wire() for name, value in values.items()}, indent=2),
            )

    def _load(self) -> dict[str, AgentRuntimeInfo]:
        with _store_lock(self._path):
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, AgentRuntimeInfo]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text())
        return {name: AgentRuntimeInfo.from_wire(data) for name, data in raw.items()}


class MessageType(Enum):
    INFO = "info"
    QUESTION = "question"
    ACK = "ack"
    HANDOFF = "handoff"
    ALERT = "alert"


GLOBAL_CHANNEL = "#all"
BROADCAST_ALIASES = frozenset({GLOBAL_CHANNEL, "broadcast"})
_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def is_channel_target(target: str) -> bool:
    """A ``#``-prefixed channel (``#all`` is the global channel)."""
    return target.startswith("#")


def channel_tag(target: str) -> str:
    """Tag name behind a ``#``-channel target. ``#all`` -> ``all``."""
    return target[1:]


# ─── Thread ───────────────────────────────────────────────────────────────────
# Declaring a Thread proves name well-formedness and that a fork is not its
# own parent. Every Thread in the system derives its semantics from this type.


@dataclass(frozen=True, slots=True)
class Goal:
    """One durable objective shared by its executing owner and all clients."""

    text: str
    id: str
    status: str = "active"
    progress: str = ""
    # Older registry rows omit this field and start at revision zero. Every
    # later goal transition advances it, even when status/progress return to
    # identical values, so a captured Goal cannot pass a stale CAS after ABA.
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.id:
            raise ValueError("A goal requires text and an identity.")
        if self.status not in {"active", "paused", "blocked", "completed"}:
            raise ValueError("Unknown goal status.")
        if type(self.revision) is not int or not 0 <= self.revision < 1 << 63:
            raise ValueError("Goal revision must be an exact nonnegative 63-bit integer.")

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def toggle_action(self) -> str:
        return "paused" if self.active else "active"

    @property
    def toggle_label(self) -> str:
        return "Pause" if self.active else "Resume"

    @property
    def summary(self) -> str:
        return f"Goal · {self.status}: {self.text}"


class BuiltinChannel(StrEnum):
    ANY = "#any"
    NONE = "#none"
    ALL = "#all"

    @classmethod
    def lookup(cls, name: str) -> BuiltinChannel | None:
        return next((channel for channel in cls if channel.value == name), None)

    @property
    def aggregate(self) -> bool:
        return self is self.ANY

    def matches(self, tags: frozenset[str]) -> bool:
        return self is not self.NONE or not tags

    @property
    def history_targets(self) -> frozenset[str] | None:
        if self.aggregate:
            return None
        return frozenset({self.value, "broadcast"}) if self is self.ALL else frozenset({self.value})


@dataclass(frozen=True, slots=True)
class Tag:
    name: str

    def __post_init__(self) -> None:
        if (
            not self.name
            or not set(self.name) <= _TAG_CHARS
            or BuiltinChannel.lookup(f"#{self.name}")
        ):
            raise ValueError(
                "Tags must be lowercase alphanumeric with hyphens/underscores; "
                "built-in channel names are reserved."
            )


@dataclass(frozen=True, slots=True)
class Channel:
    """One routable target and its presentation-only metadata.

    Legacy declarations may still carry several tags as a compatibility
    audience. Exact one-tag channels are always projected independently by the
    catalog; ``parent`` and ``archived`` never participate in matching.
    """

    name: str
    tags: frozenset[str] = frozenset()
    order: ThreadSort = field(default_factory=lambda: ThreadSort.CREATED)
    created_at: float = 0
    pinned: bool = False
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False

    def __post_init__(self) -> None:
        name = self.name if self.name.startswith("#") else f"#{self.name}"
        object.__setattr__(self, "name", name)
        if self.parent is not None:
            parent = self.parent if self.parent.startswith("#") else f"#{self.parent}"
            object.__setattr__(self, "parent", parent)
        if not isinstance(self.order, ThreadSort):
            raise ValueError("Channel ordering must be a declared ThreadSort.")
        if not isinstance(self.archived, bool) or not isinstance(self.any_mode, bool):
            raise ValueError("Channel presentation flags must be boolean.")
        if self.any_mode and not self.exact:
            raise ValueError("Only exact tag channels can include member activity.")
        if not name[1:] or not set(name[1:]) <= _TAG_CHARS:
            raise ValueError(
                "Channel names must be lowercase alphanumeric with hyphens/underscores."
            )
        if self.parent is not None and (
            not self.parent[1:] or not set(self.parent[1:]) <= _TAG_CHARS
        ):
            raise ValueError(
                "Channel parents must be lowercase alphanumeric with hyphens/underscores."
            )
        if self.parent == name:
            raise RelationViolationError("A channel cannot be its own parent.")
        if self.builtin is not None:
            if self.tags:
                raise ValueError("Built-in channels cannot have tag filters.")
            if self.parent is not None or self.archived:
                raise ValueError("Built-in channels cannot be grouped or archived.")
        elif not self.tags:
            raise ValueError("A channel requires at least one tag.")
        for tag in self.tags:
            Tag(tag)

    def matches(self, tags: frozenset[str]) -> bool:
        return self.builtin.matches(tags) if self.builtin else bool(self.tags & tags)

    @property
    def builtin(self) -> BuiltinChannel | None:
        return BuiltinChannel.lookup(self.name)

    @property
    def aggregate(self) -> bool:
        return self.builtin is not None and self.builtin.aggregate

    @property
    def exact(self) -> bool:
        return self.builtin is None and self.tags == frozenset({self.name.removeprefix("#")})

    def to_wire(self) -> dict[str, object]:
        return {**asdict(self), "tags": sorted(self.tags), "order": self.order.value}


class ViewKind(StrEnum):
    PARTICIPANTS = "participants"
    ACTIVITY = "activity"


class ViewMatch(StrEnum):
    ANY_OF = "any_of"
    ALL_OF = "all_of"


@dataclass(frozen=True, slots=True)
class ViewPredicate:
    """A typed tag predicate; expression syntax is deliberately unsupported."""

    match: ViewMatch
    tags: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "match", ViewMatch(self.match))
        if not self.tags:
            raise ValueError("A view predicate requires at least one tag.")
        for tag in self.tags:
            Tag(tag)

    def matches(self, tags: frozenset[str]) -> bool:
        if self.match is ViewMatch.ALL_OF:
            return self.tags <= tags
        return bool(self.tags & tags)

    def to_wire(self) -> dict[str, object]:
        return {"match": self.match.value, "tags": sorted(self.tags)}

    @classmethod
    def from_wire(cls, value: Mapping) -> Self:
        return cls(ViewMatch(str(value["match"])), frozenset(map(str, value["tags"])))


@dataclass(frozen=True, slots=True)
class SavedView:
    """A named non-routable projection over authoritative channels or activity."""

    name: str
    kind: ViewKind
    predicate: ViewPredicate
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ViewKind(self.kind))
        if (
            not self.name
            or self.name.startswith("#")
            or not set(self.name) <= _TAG_CHARS
            or BuiltinChannel.lookup(f"#{self.name}")
        ):
            raise ValueError(
                "View names must be lowercase alphanumeric with hyphens/underscores "
                "and cannot be channel targets."
            )

    def matches(self, tags: frozenset[str]) -> bool:
        return self.predicate.matches(tags)

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "predicate": self.predicate.to_wire(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_wire(cls, name: str, value: Mapping) -> Self:
        return cls(
            name,
            ViewKind(str(value["kind"])),
            ViewPredicate.from_wire(value["predicate"]),
            float(value.get("created_at", 0)),
        )


class ThreadRole(StrEnum):
    AGENT = "agent"
    USER = "user"

    @property
    def executable(self) -> bool:
        return self is self.AGENT


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    id: str
    owner_pid: int
    started_at: float = field(default_factory=time.time)
    routing: TurnRouting | None = None

    @classmethod
    def from_wire(cls, data: Mapping) -> ActiveTurn:
        return cls(
            data["id"],
            data["owner_pid"],
            data["started_at"],
            TurnRouting.from_wire(data["routing"]) if data.get("routing") else None,
        )

    def to_wire(self) -> dict[str, object]:
        return {**asdict(self), "routing": self.routing.to_wire() if self.routing else None}


class _GeneratedCreationTime(float):
    """Transient marker for default timestamps; never persisted as claim authority."""


def _thread_creation_time() -> float:
    return _GeneratedCreationTime(time.time())


@dataclass(frozen=True, slots=True)
class Thread:
    """Declares one agent thread's identity and provenance."""

    name: str
    tags: frozenset[str]
    worktree: str
    parent: str | None = None
    task: str | None = None
    pid: int = 0
    session_file: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    goal: Goal | None = None
    created_at: float = field(default_factory=_thread_creation_time)
    _generated_created_at: bool = field(init=False, default=False, repr=False, compare=False)
    previous_worktrees: tuple[str, ...] = ()
    auto_title_pending: bool = False
    title: str | None = None
    role: ThreadRole = ThreadRole.AGENT
    active_turn: ActiveTurn | None = None

    def __post_init__(self) -> None:
        generated = isinstance(self.created_at, _GeneratedCreationTime)
        object.__setattr__(self, "_generated_created_at", generated)
        if generated:
            object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "role", ThreadRole(self.role))
        if self.active_turn is not None and self.active_turn.owner_pid != self.pid:
            raise RelationViolationError("A turn must belong to the registered executor.")
        for tag in self.tags:
            Tag(tag)
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not self.name or not set(self.name) <= allowed:
            raise ValueError(
                f"Thread name {self.name!r} must be alphanumeric with hyphens/underscores."
            )
        if self.parent == self.name:
            raise RelationViolationError(f"Thread {self.name!r} cannot be its own parent.")
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")
        if self.model is not None and not self.model.strip():
            raise ValueError("Thread model cannot be empty.")
        if self.thinking_level is not None and self.thinking_level not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Unknown thinking level.")

    @property
    def is_fork(self) -> bool:
        return self.parent is not None

    @property
    def executing(self) -> bool:
        return self.active_turn is not None

    def to_wire(self) -> dict[str, object]:
        """Schema-derived projection at a JSON boundary, not a hand-maintained mirror."""
        values = asdict(self)
        values.pop("_generated_created_at")  # construction provenance is not durable authority
        return {
            **values,
            "tags": sorted(self.tags),
            "active_turn": self.active_turn.to_wire() if self.active_turn else None,
        }


@dataclass(frozen=True, slots=True)
class ChannelView:
    channel: Channel
    members: tuple[str, ...]
    last_activity: float = 0
    last_user_input: float = 0
    pinned_members: frozenset[str] = frozenset()

    def to_wire(self) -> dict[str, object]:
        return {
            **self.channel.to_wire(),
            "members": list(self.members),
            "last_activity": self.last_activity,
            "last_user_input": self.last_user_input,
            "pinned_members": sorted(self.pinned_members),
        }


@dataclass(frozen=True, slots=True)
class ThreadView:
    thread: Thread
    status: ThreadStatus
    activity: Activity
    runtime: AgentRuntimeInfo | None
    last_seen: float

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        return self.status.presentation(self.thread.title or self.thread.name, self.activity)

    def to_wire(self) -> dict[str, object]:
        return {
            **self.thread.to_wire(),
            "status": self.status.value,
            "is_fork": self.thread.is_fork,
            "resumable": bool(self.thread.session_file),
            "last_seen": self.last_seen,
            "last_activity": self.activity.timestamp,
            "activity": self.activity.state.value,
            "activity_detail": self.activity.detail,
            "model": self.runtime.model if self.runtime else self.thread.model,
            "session_name": self.runtime.session_name if self.runtime else None,
            "context_used": self.runtime.context_used if self.runtime else None,
            "context_size": self.runtime.context_size if self.runtime else None,
            "context_percent": self.runtime.context_percent if self.runtime else None,
        }


@dataclass(frozen=True, slots=True)
class ThreadPresentation:
    title: str
    marker: str
    summary: str
    busy: bool = False

    @property
    def label(self) -> str:
        return f"{self.marker} {self.title}"


class DisplayOrder(Enum):
    """A persisted sorting choice with its adapter-independent display label."""

    def __new__(cls, key: str, label: str = "") -> Self:
        member = object.__new__(cls)
        member._value_ = key
        member.label = label
        return member

    label: str


class ThreadSort(DisplayOrder):
    LAST_MESSAGE = "last_message_sent", "Last message sent"
    CREATED = "created_at", "Date created"
    LAST_ACTIVITY = "last_activity", "Last activity"

    @classmethod
    def resolve(cls, value: str) -> ThreadSort:
        try:
            return cls(value)
        except ValueError:
            return cls.CREATED

    def key(
        self, name: str, created: float, activity: float, sent: float
    ) -> tuple[float, float, str]:
        timestamp = {
            self.CREATED: created,
            self.LAST_ACTIVITY: activity,
            self.LAST_MESSAGE: sent,
        }[self]
        return -timestamp, -created, name.casefold()


class ChannelSort(DisplayOrder):
    NAME = "name", "Name"
    CREATED = "created_at", "Date created"
    LAST_ACTIVITY = "last_activity", "Last activity"
    LAST_USER_INPUT = "last_user_input", "Last user input"

    def key(self, view: ChannelView) -> tuple[int, int, float, float, str]:
        builtins = tuple(BuiltinChannel)
        builtin = view.channel.builtin
        if builtin is not None:
            return (
                not view.channel.pinned,
                builtins.index(builtin),
                0,
                0,
                view.channel.name,
            )
        timestamp = {
            self.NAME: 0,
            self.CREATED: view.channel.created_at,
            self.LAST_ACTIVITY: view.last_activity,
            self.LAST_USER_INPUT: view.last_user_input,
        }[self]
        created = 0 if self is self.NAME else view.channel.created_at
        return (
            not view.channel.pinned,
            len(builtins),
            -timestamp,
            -created,
            view.channel.name.casefold(),
        )


@dataclass(frozen=True, slots=True)
class ChannelActivity:
    last_message: float = 0
    last_user_input: float = 0

    def observe(self, message: Message) -> ChannelActivity:
        return ChannelActivity(
            max(self.last_message, message.timestamp),
            (
                max(self.last_user_input, message.timestamp)
                if message.sender_role is ThreadRole.USER
                else self.last_user_input
            ),
        )


@dataclass(frozen=True, slots=True)
class CoordinationSnapshot:
    threads: tuple[ThreadView, ...]
    channels: tuple[ChannelView, ...]
    unread: Mapping[str, int]
    last_sent: Mapping[str, float]
    channel_unread: Mapping[str, int] = field(default_factory=dict)
    channel_order: ChannelSort = ChannelSort.NAME
    thread_unread: Mapping[str, int] = field(default_factory=dict)
    show_stopped: bool = True
    show_archived: bool = False

    def participants(self, channel: str) -> tuple[ThreadView, ...]:
        view = next((view for view in self.channels if view.channel.name == channel), None)
        if view is None:
            return ()
        people = {
            person.thread.name: person
            for person in self.threads
            if person.status.active and person.thread.executing
        }
        return tuple(people[name] for name in view.members if name in people)

    def mention_candidates(self, channel: str) -> tuple[MentionCandidate, ...]:
        view = next((view for view in self.channels if view.channel.name == channel), None)
        members = frozenset(view.members) if view else frozenset()
        return tuple(
            sorted(
                (
                    MentionCandidate(person.thread.name, person.presentation.title)
                    for person in self.threads
                    if person.thread.name in members
                ),
                key=lambda candidate: candidate.name.casefold(),
            )
        )


@dataclass(frozen=True, slots=True)
class WireRevision:
    files: tuple[tuple[int, int, int, int] | None, ...]
    expiry_tick: int


# ─── Message ──────────────────────────────────────────────────────────────────
# Declaring a Message proves sender, target, and body are present and distinct.
# The registry proves the required relation (sender and target are registered)
# at send time.


class MembershipChange(StrEnum):
    JOINED = "joined"
    LEFT = "left"


class ResponsePolicy(StrEnum):
    """Who may answer one message; delivery and history remain independent."""

    DIRECT = "direct"
    COLLECTIVE = "collective"
    MENTIONED_ONLY = "mentioned_only"
    INFORMATIONAL = "informational"


@dataclass(frozen=True, slots=True)
class ResponseEligibility:
    policy: ResponsePolicy
    recipients: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy", ResponsePolicy(self.policy))
        if len(self.recipients) != len(set(self.recipients)):
            raise ValueError("Response eligibility recipients must be unique.")
        if self.policy in {ResponsePolicy.INFORMATIONAL, ResponsePolicy.DIRECT} and self.recipients:
            raise ValueError(f"{self.policy.value} eligibility cannot declare channel recipients.")


@dataclass(frozen=True, slots=True)
class Message:
    """Declares one inter-thread message.

    The bus assigns ``seq`` (a monotonically increasing log position) at send
    time; the hash-derived ``message_id`` is identity only and is not ordered.
    """

    sender: str
    target: str
    body: str
    type: MessageType
    timestamp: float = field(default_factory=time.time)
    seq: int = 0
    sender_role: ThreadRole = ThreadRole.AGENT
    membership: MembershipChange | None = None
    notice: bool = False
    mentions: tuple[ThreadMention, ...] = ()
    claim_transition: ClaimTransition | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sender_role", ThreadRole(self.sender_role))
        if self.membership is not None:
            object.__setattr__(self, "membership", MembershipChange(self.membership))
        if not self.sender:
            raise RelationViolationError("Message sender cannot be empty.")
        if not self.target:
            raise RelationViolationError("Message target cannot be empty.")
        if not self.body:
            raise ValueError("Message body cannot be empty.")
        if self.claim_transition is not None:
            transition = self.claim_transition
            if type(transition) is not ClaimTransition or (
                self.seq > 0
                and (
                    transition.owner != self.sender
                    or transition.seq != self.seq
                    or transition.message_id != self.message_id
                )
            ):
                raise RelationViolationError("Claim transition does not bind this message.")
        if any(
            not (0 <= mention.start < mention.end <= len(self.body))
            or self.body[mention.start] != "@"
            for mention in self.mentions
        ):
            raise ValueError("Mention ranges must identify text in the message body.")
        if not is_channel_target(self.target) and self.target != "broadcast":
            # DM: a thread name; sending to yourself is not a conversation.
            if self.sender == self.target:
                raise RelationViolationError(f"Thread {self.sender!r} cannot message itself.")
            allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
            if not set(self.target) <= allowed:
                raise ValueError(
                    f"Message target {self.target!r} is not a thread name or #channel."
                )
        if self.target.startswith("#") and self.target != GLOBAL_CHANNEL:
            tag = self.target[1:]
            if not tag or not set(tag) <= _TAG_CHARS:
                raise ValueError(
                    f"Channel {self.target!r} must be lowercase alphanumeric"
                    " with hyphens/underscores."
                )

    @property
    def message_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.sender}:{self.target}:{self.timestamp}:{self.body}".encode()
        ).hexdigest()[:12]
        return digest

    def to_wire(self) -> dict:
        return {
            "seq": self.seq,
            "id": self.message_id,
            "from": self.sender,
            "to": self.target,
            "ts": self.timestamp,
            "type": self.type.value,
            "text": self.body,
            "sender_role": self.sender_role.value,
            **({"membership": self.membership.value} if self.membership is not None else {}),
            **({"notice": True} if self.notice else {}),
            **(
                {"mentions": [asdict(mention) for mention in self.mentions]}
                if self.mentions
                else {}
            ),
            **(
                {"claim_transition": _claim_transition_wire(self.claim_transition)}
                if self.claim_transition is not None
                else {}
            ),
        }

    @classmethod
    def from_wire(cls, data: Mapping) -> Message:
        return cls(
            sender=data["from"],
            target=data["to"],
            body=data["text"],
            type=MessageType(data["type"]),
            timestamp=data.get("ts", 0.0),
            seq=int(data.get("seq", 0)),
            sender_role=ThreadRole(data.get("sender_role", ThreadRole.AGENT.value)),
            membership=MembershipChange(data["membership"]) if data.get("membership") else None,
            notice=bool(data.get("notice", False)),
            mentions=tuple(ThreadMention.from_wire(item) for item in data.get("mentions", ())),
            claim_transition=(
                _claim_transition_from_wire(data["claim_transition"])
                if "claim_transition" in data
                else None
            ),
        )

    @property
    def response_policy(self) -> ResponsePolicy:
        """Typed response semantics without changing the stored target."""
        if self.notice or self.membership is not None:
            return ResponsePolicy.INFORMATIONAL
        if not (is_channel_target(self.target) or self.target in BROADCAST_ALIASES):
            return ResponsePolicy.DIRECT
        if self.mentions:
            return ResponsePolicy.MENTIONED_ONLY
        if not self.sender_role.executable:
            return ResponsePolicy.COLLECTIVE
        return ResponsePolicy.INFORMATIONAL

    def response_eligibility(self, audience: Sequence[str]) -> ResponseEligibility:
        """Resolve channel responders from canonical audience identities.

        Mention identities were canonicalized when the message was committed.
        The audience remains authoritative for membership, so a valid mention
        outside the channel cannot acquire delivery by being named.
        """
        policy = self.response_policy
        if policy is ResponsePolicy.COLLECTIVE:
            recipients = tuple(dict.fromkeys(audience))
        elif policy is ResponsePolicy.MENTIONED_ONLY:
            selected = frozenset(mention.thread for mention in self.mentions)
            recipients = tuple(name for name in dict.fromkeys(audience) if name in selected)
        else:
            recipients = ()
        return ResponseEligibility(policy, recipients)

    @property
    def starts_turn(self) -> bool:
        """Whether every delivered recipient may start a turn."""
        return self.response_policy in {ResponsePolicy.DIRECT, ResponsePolicy.COLLECTIVE}

    def starts_turn_for(self, name: str) -> bool:
        """Whether this message enters one canonical recipient's model context."""
        channel = is_channel_target(self.target) or self.target in BROADCAST_ALIASES
        if not channel:
            return self.starts_turn
        policy = self.response_policy
        if policy is ResponsePolicy.COLLECTIVE:
            return True
        if policy is ResponsePolicy.MENTIONED_ONLY:
            return any(mention.thread == name for mention in self.mentions)
        return False

    @property
    def reply_target(self) -> str | None:
        if self.sender_role.executable:
            return None
        return self.target if is_channel_target(self.target) else self.sender


@dataclass(frozen=True, slots=True)
class MessageRoute:
    sender: str
    targets: tuple[str, ...]

    @property
    def outgoing_label(self) -> str:
        return "To " + ", ".join(self.targets)

    @property
    def incoming_scope(self) -> str:
        return (
            ", ".join(
                target if is_channel_target(target) else "direct message" for target in self.targets
            )
            or "direct message"
        )

    @property
    def incoming_label(self) -> str:
        return f"Incoming from {self.sender}: via {self.incoming_scope}"

    @classmethod
    def from_wire(cls, data: Mapping) -> MessageRoute:
        return cls(str(data["sender"]), tuple(str(target) for target in data["targets"]))


@dataclass(frozen=True, slots=True)
class TurnRouting:
    requests: tuple[Message, ...] = ()
    reply: MessageRoute | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "requests": [message.to_wire() for message in self.requests],
            "reply": asdict(self.reply) if self.reply else None,
        }

    @classmethod
    def from_wire(cls, data: Mapping) -> TurnRouting:
        return cls(
            tuple(Message.from_wire(item) for item in data.get("requests", [])),
            MessageRoute.from_wire(data["reply"]) if data.get("reply") else None,
        )


@dataclass(frozen=True, slots=True)
class ScheduledTurn:
    prompt: str
    origin: Message | None = None
    goal_id: str | None = None

    @property
    def reply_target(self) -> str | None:
        return self.origin.reply_target if self.origin else None

    @classmethod
    def incoming(cls, message: Message) -> ScheduledTurn:
        policy = message.response_policy
        if policy is ResponsePolicy.COLLECTIVE:
            guidance = "collective; channel members may respond"
        elif policy is ResponsePolicy.MENTIONED_ONLY:
            names = ", ".join(dict.fromkeys(f"@{mention.thread}" for mention in message.mentions))
            guidance = (
                f"mentioned_only; only resolved mentioned identities may respond: {names}; "
                "unmentioned observers dismiss quietly"
            )
        elif policy is ResponsePolicy.INFORMATIONAL:
            guidance = "informational; observe and dismiss without replying"
        else:
            guidance = "direct; reply to the sender"
        scope = (
            f"; delivery and history remain {message.target}"
            if is_channel_target(message.target)
            else ""
        )
        return cls(
            f"[agent-comms from {message.sender} to {message.target}]\n"
            f"[Response policy: {guidance}{scope}]\n{message.body}",
            origin=message,
        )

    @staticmethod
    def take_batch(pending: list[ScheduledTurn]) -> tuple[list[ScheduledTurn], list[ScheduledTurn]]:
        """Only combine adjacent requests whose answers have the same destination."""
        if not pending:
            return [], []
        boundary = next(
            (
                index
                for index, turn in enumerate(pending)
                if turn.reply_target != pending[0].reply_target
            ),
            len(pending),
        )
        return pending[:boundary], pending[boundary:]


@dataclass(frozen=True, slots=True)
class MessagePage:
    """A bounded, ascending page from the durable message log.

    Cursors are message sequence numbers and are exclusive when passed back as
    ``before`` or ``after``. A single oversized message is returned by itself
    so every cursor can make progress despite the byte budget.
    """

    messages: tuple[Message, ...]
    has_older: bool
    has_newer: bool

    def __post_init__(self) -> None:
        sequences = [message.seq for message in self.messages]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("Message pages must contain unique messages in seq order.")

    @property
    def oldest_seq(self) -> int | None:
        return self.messages[0].seq if self.messages else None

    @property
    def newest_seq(self) -> int | None:
        return self.messages[-1].seq if self.messages else None


@dataclass(frozen=True, slots=True)
class DeliveryScope:
    actor: str
    aliases: Mapping[str, str]
    channels: frozenset[str]

    def canonical(self, name: str) -> str:
        return self.aliases.get(name, name)

    def delivers(self, message: Message) -> bool:
        return self.canonical(message.sender) != self.actor and (
            message.target in self.channels or self.canonical(message.target) == self.actor
        )

    def conversation(self, message: Message) -> str:
        if is_channel_target(message.target):
            return message.target
        sender, target = self.canonical(message.sender), self.canonical(message.target)
        return sender if target == self.actor else target


@dataclass(frozen=True, slots=True)
class PendingCounts:
    revision: tuple[tuple[int, int, int, int] | None, ...]
    delivery: DeliveryScope
    counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ChannelReadScope:
    channel: str
    targets: frozenset[str] | None
    after: int

    def unread(self, message: Message) -> bool:
        return message.seq > self.after and (self.targets is None or message.target in self.targets)


@dataclass(frozen=True, slots=True)
class ChannelDisplayScope:
    """One captured local display predicate; it never changes delivery or history ownership."""

    channel: str
    targets: frozenset[str] | None
    any_mode: bool = False
    participant_names: frozenset[str] = frozenset()
    after: int = 0
    basis_revision: tuple = ()

    def includes(self, message: Message) -> bool:
        if self.targets is None or message.target in self.targets:
            return True
        if not self.any_mode:
            return False
        return (
            message.sender in self.participant_names
            or (
                not is_channel_target(message.target)
                and message.target != "broadcast"
                and message.target in self.participant_names
            )
            or any(mention.thread in self.participant_names for mention in message.mentions)
        )

    def unread(self, message: Message) -> bool:
        return message.seq > self.after and self.includes(message)


@dataclass(frozen=True, slots=True)
class ViewUnread:
    revision: tuple | None
    scopes: tuple[ChannelReadScope | ChannelDisplayScope, ...]
    counts: Mapping[str, int]
    verified_display_boundary: bool = False


# ─── Thread Registry ──────────────────────────────────────────────────────────
# Persists Thread instances and their statuses. Fail-closed: referencing an
# unregistered thread raises UnregisteredThreadError.


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    threads: Mapping[str, Thread]
    statuses: Mapping[str, ThreadStatus]
    last_seen: Mapping[str, float]
    aliases: Mapping[str, str]
    owner_epochs: Mapping[str, int]
    turn_epochs: Mapping[str, int]


class ThreadRegistry:
    """Persists threads and manages status transitions and presence."""

    def __init__(self, store_path: Path):
        self._path = store_path
        self._threads: dict[str, Thread] = {}
        self._statuses: dict[str, ThreadStatus] = {}
        self._last_seen: dict[str, float] = {}
        self._aliases: dict[str, str] = {}
        self._revision: tuple[int, int, int, int] | None = None
        # Private registry metadata, never a Thread wire field or public bus field.
        self._owner_epochs: dict[str, int] = {}
        # Only an atomic turn claim may create this private attestation. A
        # registration cannot restore a saved ActiveTurn after it was revoked.
        self._turn_epochs: dict[str, int] = {}
        self._owner_epoch_counter = 0
        self._epoch_metadata_present = False
        self._load()

    def _load(self) -> None:
        with _store_lock(self._path):
            self._load_unlocked()

    def _private_guard_unlocked(self) -> PrivateRegistryGuard | None:
        """Validate the private marker/guard BEFORE even a cached registry read.

        The directory, guard file and registry are one private root. A marker
        without its committed guard (or a guard without a marker) is an
        uncertain cutover, never an invitation to bootstrap old metadata.
        """
        from .private_registry_guard import PrivateRegistryGuard

        marker_path = self._path.parent / "bus_meta.json"
        guard_path = self._path.parent / ".registry-owner-guard"
        guard_present = guard_path.exists() or guard_path.is_symlink()
        if not marker_path.exists() and not marker_path.is_symlink():
            if guard_present:
                raise RelationViolationError("Private registry guard has no protocol marker")
            return None
        try:
            marker = json.loads(marker_path.read_text(), object_pairs_hook=unique_wire_object)
        except (OSError, ValueError, UnicodeError) as error:
            raise RelationViolationError("Private registry guard marker is invalid") from error
        if type(marker) is not dict:
            raise RelationViolationError("Private registry guard marker is not an object")
        if "writer_protocol_version" not in marker:
            if guard_present:
                raise RelationViolationError("Private registry guard marker is absent")
            return None
        if (
            set(marker)
            not in (
                {"last_seq", "writer_protocol_version", "wire_root_id"},
                {"last_seq", "writer_protocol_version", "wire_root_id", "claim_envelopes_version"},
            )
            or (
                "claim_envelopes_version" in marker
                and (
                    type(marker["claim_envelopes_version"]) is not int
                    or marker["claim_envelopes_version"] != 1
                )
            )
            or type(marker["writer_protocol_version"]) is not int
            or marker["writer_protocol_version"] != 1
            or type(marker["last_seq"]) is not int
            or not 0 <= marker["last_seq"] < 1 << 63
        ):
            raise RelationViolationError("Private registry guard marker is malformed")
        marker_info = marker_path.lstat()
        if (
            not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_uid != os.geteuid()
            or stat.S_IMODE(marker_info.st_mode) != 0o600
            or marker_info.st_nlink != 1
        ):
            raise RelationViolationError("Private registry guard marker is not owner-only")
        root_id = marker.get("wire_root_id")
        if type(root_id) is not str:
            raise RelationViolationError("Private registry guard root ID is invalid")
        guard = PrivateRegistryGuard(self._path, root_id)
        guard.verify()
        return guard

    def _load_unlocked(self) -> None:
        self._private_guard_unlocked()
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            revision = None
        else:
            revision = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if revision == self._revision:
                return
        # Validate the new snapshot before replacing the last successful read.
        self._revision = None
        raw = json.loads(self._path.read_text()) if revision is not None else {}
        has_epochs = "owner_epochs" in raw or "owner_epoch_counter" in raw
        if has_epochs:
            epochs = raw.get("owner_epochs")
            counter = raw.get("owner_epoch_counter")
            if (
                type(epochs) is not dict
                or type(counter) is not int
                or counter < 0
                or any(
                    type(name) is not str or type(epoch) is not int or not 0 < epoch <= counter
                    for name, epoch in epochs.items()
                )
            ):
                raise RelationViolationError("invalid private registry owner epochs")
            turns = raw.get("turn_epochs", {})
            if type(turns) is not dict or any(
                type(name) is not str
                or type(epoch) is not int
                or epoch < 1
                or epochs.get(name) != epoch
                for name, epoch in turns.items()
            ):
                raise RelationViolationError("invalid private registry turn epochs")
            self._owner_epochs = dict(epochs)
            self._turn_epochs = dict(turns)
            self._owner_epoch_counter = counter
        else:
            # A private wire marker is issued only on a fresh root. Missing
            # epochs after that marker are a downgrade, never legacy migration:
            # an old writer may have removed them without changing the owner.
            marker_path = self._path.parent / "bus_meta.json"
            if marker_path.exists() or marker_path.is_symlink():
                try:
                    marker = json.loads(marker_path.read_text())
                except (OSError, ValueError, UnicodeError) as error:
                    raise RelationViolationError("private wire marker is unreadable") from error
                if type(marker) is dict and "writer_protocol_version" in marker:
                    # Marker-before-participant initialization is valid only
                    # before any bus row or registry snapshot has existed.
                    fresh_empty_root = (
                        revision is None
                        and marker.get("last_seq") == 0
                        and not (self._path.parent / "bus.jsonl").exists()
                    )
                    if not fresh_empty_root:
                        raise RelationViolationError("private owner epoch metadata was lost")
            # Unmarked legacy stores remain readable but cannot authorize the
            # coordinated CAS until an explicit owner registration migrates them.
            legacy = raw.get("threads", {})
            if type(legacy) is not dict or any(type(name) is not str for name in legacy):
                raise RelationViolationError("invalid legacy registry owner declarations")
            self._owner_epochs = {name: index for index, name in enumerate(sorted(legacy), start=1)}
            self._turn_epochs = {}
            self._owner_epoch_counter = len(self._owner_epochs)
        self._epoch_metadata_present = has_epochs
        self._threads.clear()
        self._statuses.clear()
        self._last_seen.clear()
        self._aliases.clear()
        self._aliases.update(raw.get("aliases", {}))
        for name, data in raw.get("threads", {}).items():
            self._threads[name] = Thread(
                name=name,
                tags=frozenset(data.get("tags", [])),
                worktree=data.get("worktree", ""),
                parent=data.get("parent"),
                task=data.get("task"),
                pid=data.get("pid", 0),
                session_file=data.get("session_file"),
                model=data.get("model"),
                thinking_level=data.get("thinking_level"),
                goal=Goal(**data["goal"]) if data.get("goal") else None,
                created_at=self._created_at(data),
                previous_worktrees=tuple(data.get("previous_worktrees", [])),
                auto_title_pending=bool(data.get("auto_title_pending", False)),
                title=data.get("title"),
                role=ThreadRole(data.get("role", ThreadRole.AGENT.value)),
                active_turn=(
                    ActiveTurn.from_wire(data["active_turn"]) if data.get("active_turn") else None
                ),
            )
            self._statuses[name] = ThreadStatus(data.get("status", "running"))
            self._last_seen[name] = data.get("last_seen", 0.0)
            if has_epochs and name not in self._owner_epochs:
                raise RelationViolationError("missing private registry owner epoch")
        if any(
            name not in self._threads or self._threads[name].active_turn is None
            for name in self._turn_epochs
        ):
            raise RelationViolationError("private registry turn attestation has no live turn")
        # Older stores retained aliases after deletion. Only a retained thread
        # (including an archived one) can own a name reservation.
        self._aliases = {
            alias: target for alias, target in self._aliases.items() if target in self._threads
        }
        self._revision = revision

    @staticmethod
    @lru_cache(maxsize=512)
    def _session_created_at(session_file: str) -> float:
        # Pi session headers are immutable; avoid reopening legacy transcripts
        # on every registry lookup before their creation date is persisted.
        with Path(session_file).open("rb") as stream:
            line = stream.readline(8192)
        try:
            header = json.loads(line)
            if header.get("type") == "session" and header.get("timestamp"):
                return datetime.fromisoformat(
                    header["timestamp"].replace("Z", "+00:00")
                ).timestamp()
        except (ValueError, TypeError, AttributeError):
            pass
        return 0.0

    @staticmethod
    def _created_at(data: Mapping) -> float:
        """Read old session headers when a registry predates creation timestamps."""
        if "created_at" in data:
            return float(data["created_at"])
        if session_file := data.get("session_file"):
            try:
                return ThreadRegistry._session_created_at(session_file)
            except OSError:
                pass
        # Unknown legacy creation dates sort oldest, never by a mutable heartbeat.
        return 0.0

    def _bump_owner_epoch_unlocked(self, name: str) -> None:
        self._turn_epochs.pop(name, None)
        self._owner_epoch_counter += 1
        self._owner_epochs[name] = self._owner_epoch_counter

    def _save_unlocked(self) -> None:
        # A failed write must never make speculative in-memory mutations authoritative.
        self._revision = None
        guard = self._private_guard_unlocked()
        serialized = json.dumps(
            {
                "threads": {
                    name: {
                        **t.to_wire(),
                        "status": self._statuses.get(name, ThreadStatus.RUNNING).value,
                        "last_seen": self._last_seen.get(name, 0.0),
                    }
                    for name, t in self._threads.items()
                },
                "aliases": dict(sorted(self._aliases.items())),
                "owner_epoch_counter": self._owner_epoch_counter,
                "owner_epochs": dict(sorted(self._owner_epochs.items())),
                "turn_epochs": dict(sorted(self._turn_epochs.items())),
            },
            indent=2,
        )
        if guard is None:
            _atomic_write_text(self._path, serialized)
        else:
            digest = hashlib.sha256(b"present\0" + serialized.encode("utf-8")).digest()
            sequence, slot = guard.prepare(digest)
            _atomic_write_text(self._path, serialized, fsync_parent=True)
            guard.commit(sequence, slot, digest)

    def register(self, thread: Thread, status: ThreadStatus = ThreadStatus.RUNNING) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            if thread.name in self._aliases:
                raise RelationViolationError(
                    f"Thread name {thread.name!r} is a permanent alias and cannot be reused."
                )
            if not self._statuses.get(thread.name, ThreadStatus.RUNNING).mutable:
                raise RelationViolationError(
                    f"Thread {thread.name!r} is being permanently deleted."
                )
            if previous := self._threads.get(thread.name):
                thread = replace(thread, created_at=previous.created_at)
            elif any(
                existing.created_at == thread.created_at for existing in self._threads.values()
            ):
                # The Windows wall clock can return the same value for six
                # independent default-constructed threads. Allocate a distinct
                # identity under this store lock, but never rewrite an explicit
                # caller-supplied creation identity or alias someone else's claim.
                if not thread._generated_created_at:
                    raise RelationViolationError("Registry creation identities collide.")
                used = {existing.created_at for existing in self._threads.values()}
                candidate = float(thread.created_at)
                while candidate in used:
                    candidate = math.nextafter(candidate, math.inf)
                if not math.isfinite(candidate):
                    raise RelationViolationError("Registry creation identities collide.")
                thread = replace(thread, created_at=candidate)
            self._threads[thread.name] = thread
            self._statuses[thread.name] = status
            self._last_seen[thread.name] = time.time()
            self._bump_owner_epoch_unlocked(thread.name)
            self._save_unlocked()

    def live_owner_with_epoch(self, name: str) -> tuple[Thread, int]:
        """Capture an active owner and its persistent incarnation under one lock.

        Unlike a global file revision, an unrelated recipient's claim cannot
        invalidate this owner's attempt. A stop then heartbeat changes its epoch
        even if the declaration, PID, and status return to their earlier values.
        """
        with _store_lock(self._path):
            self._load_unlocked()
            canonical = self._aliases.get(name, name)
            owner = self._threads.get(canonical)
            status = self._statuses.get(canonical)
            epoch = self._owner_epochs.get(canonical)
            if (
                owner is None
                or status is None
                or not status.active
                or owner.pid != os.getpid()
                or not owner.role.executable
                or not self._epoch_metadata_present
                or epoch is None
                or (
                    owner is not None
                    and owner.active_turn is not None
                    and self._turn_epochs.get(canonical) != epoch
                )
            ):
                raise RelationViolationError("live owner is stopped or unavailable")
            return owner, epoch

    def claim_live_turn_with_epoch(
        self,
        expected: Thread,
        turn_id: str,
        *,
        expected_epoch: int,
        routing: TurnRouting | None = None,
    ) -> tuple[Thread, int]:
        """Atomically claim a fresh turn and return its new owner incarnation.

        Never sample the epoch in a second read: an owner may stop and register
        the same declaration between that read and the claim's return.
        """
        if (
            type(expected) is not Thread
            or type(turn_id) is not str
            or not 0 < len(turn_id) <= 128
            or type(expected_epoch) is not int
            or expected_epoch < 1
        ):
            raise ValueError("live owner turn requires exact identity, epoch and bounded ID")
        with _store_lock(self._path):
            self._load_unlocked()
            current = self._threads.get(expected.name)
            status = self._statuses.get(expected.name)
            if (
                not self._epoch_metadata_present
                or self._owner_epochs.get(expected.name) != expected_epoch
                or current != expected
                or status is None
                or not status.active
                or current is None
                or current.pid != os.getpid()
                or not current.role.executable
                or current.active_turn is not None
            ):
                raise RelationViolationError("live owner stopped or changed before turn claim")
            return self._claim_turn_unlocked(current, turn_id, routing)

    def _claim_turn_unlocked(
        self, current: Thread, turn_id: str, routing: TurnRouting | None
    ) -> tuple[Thread, int]:
        """Caller holds the registry lock and has checked live turn ownership."""
        claimed = replace(current, active_turn=ActiveTurn(turn_id, current.pid, routing=routing))
        self._threads[current.name] = claimed
        self._last_seen[current.name] = time.time()
        self._bump_owner_epoch_unlocked(current.name)
        claimed_epoch = self._owner_epochs[current.name]
        self._turn_epochs[current.name] = claimed_epoch
        self._save_unlocked()
        return claimed, claimed_epoch

    def claim_local_turn(
        self, name: str, turn_id: str, *, routing: TurnRouting | None = None
    ) -> tuple[Thread, int]:
        """Atomic local begin-turn, never reviving a stopped or replaced owner.

        A legacy unmarked registry can be migrated under the same lock as the
        claim. Marked private roots must not recreate missing epoch metadata:
        an old writer may have stripped it during an unsafe cutover.
        """
        if type(turn_id) is not str or not 0 < len(turn_id) <= 128:
            raise ValueError("live owner turn requires a bounded ID")
        with _store_lock(self._path):
            self._load_unlocked()
            canonical = self._aliases.get(name, name)
            current = self._threads.get(canonical)
            status = self._statuses.get(canonical)
            if (
                current is None
                or status is None
                or not status.active
                or current.pid != os.getpid()
                or not current.role.executable
                or current.active_turn is not None
            ):
                raise RelationViolationError("live owner is stopped or unavailable")
            if not self._epoch_metadata_present:
                self._epoch_metadata_present = True
            return self._claim_turn_unlocked(current, turn_id, routing)

    def claim_live_turn(self, expected: Thread, turn_id: str, *, expected_epoch: int) -> Thread:
        """Compatibility CAS result for callers that do not need the witness."""
        claimed, _ = self.claim_live_turn_with_epoch(
            expected, turn_id, expected_epoch=expected_epoch
        )
        return claimed

    def finish_claimed_turn(self, name: str, turn_id: str) -> bool:
        """Release only the exact owned turn, resolving retained aliases under lock."""
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            current = self._threads.get(name)
            if current is None or current.active_turn is None or current.active_turn.id != turn_id:
                return False
            self._threads[name] = replace(current, active_turn=None)
            self._bump_owner_epoch_unlocked(name)
            self._save_unlocked()
            return True

    def canonical_name(self, name: str) -> str:
        self._load()
        return self._aliases.get(name, name)

    def aliases_for(self, name: str) -> frozenset[str]:
        self._load()
        canonical = self._aliases.get(name, name)
        return frozenset(
            {canonical, *(alias for alias, target in self._aliases.items() if target == canonical)}
        )

    def name_reserved(self, name: str) -> bool:
        """Return whether a canonical name or permanent alias occupies text."""
        self._load()
        return name in self._threads or name in self._aliases

    def rename(self, name: str, new_name: str) -> tuple[str, str]:
        """Rename one running thread while retaining old names as aliases."""
        with _store_lock(self._path):
            self._load_unlocked()
            canonical = self._aliases.get(name, name)
            if canonical not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            if new_name == canonical:
                return canonical, canonical
            current = self._threads[canonical]
            if not self._statuses[canonical].running:
                raise RelationViolationError("Only a running thread can rename itself.")
            if new_name in self._threads or new_name in self._aliases:
                raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
            # Constructing the replacement proves the new name is valid.
            replacement = replace(current, name=new_name)
            status = self._statuses.pop(canonical)
            last_seen = self._last_seen.pop(canonical)
            del self._threads[canonical]
            self._threads[new_name] = replacement
            self._statuses[new_name] = status
            self._last_seen[new_name] = last_seen

            for child_name, child in tuple(self._threads.items()):
                if child.parent == canonical:
                    self._threads[child_name] = replace(child, parent=new_name)
            for alias, target in tuple(self._aliases.items()):
                if target == canonical:
                    self._aliases[alias] = new_name
            self._aliases[canonical] = new_name
            self._turn_epochs.pop(canonical, None)
            self._bump_owner_epoch_unlocked(new_name)
            self._save_unlocked()
            return canonical, new_name

    def unregister(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            self._statuses[name] = ThreadStatus.STOPPED
            self._threads[name] = replace(self._threads[name], active_turn=None)
            self._bump_owner_epoch_unlocked(name)
            self._save_unlocked()

    def archive(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            self._statuses[name] = ThreadStatus.ARCHIVED
            self._bump_owner_epoch_unlocked(name)
            self._save_unlocked()

    def begin_delete(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            status = self._statuses.get(name)
            if status is None or status.active:
                raise RelationViolationError(
                    "Stop a running thread before permanently deleting it."
                )
            self._statuses[name] = ThreadStatus.DELETING
            self._bump_owner_epoch_unlocked(name)
            self._save_unlocked()

    def remove(self, name: str) -> tuple[str, ...]:
        """Remove a declaration and atomically detach its surviving children."""
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            detached = tuple(
                sorted(child.name for child in self._threads.values() if child.parent == name)
            )
            for child_name in detached:
                self._threads[child_name] = replace(self._threads[child_name], parent=None)
            del self._threads[name]
            self._statuses.pop(name, None)
            self._last_seen.pop(name, None)
            # Retain the private tombstone epoch: reusing a name cannot recycle
            # an earlier owner's incarnation after deletion.
            self._bump_owner_epoch_unlocked(name)
            self._aliases = {
                alias: target for alias, target in self._aliases.items() if target != name
            }
            self._save_unlocked()
            return detached

    def heartbeat(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            if not self._statuses.get(name, ThreadStatus.RUNNING).mutable:
                raise RelationViolationError(f"Thread {name!r} is being permanently deleted.")
            if self._statuses[name] is not ThreadStatus.RUNNING:
                self._bump_owner_epoch_unlocked(name)
            self._statuses[name] = ThreadStatus.RUNNING
            self._last_seen[name] = time.time()
            self._save_unlocked()

    def last_seen(self, name: str) -> float:
        self._load()
        name = self._aliases.get(name, name)
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._last_seen.get(name, 0.0)

    def require(self, name: str) -> Thread:
        self._load()
        name = self._aliases.get(name, name)
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._threads[name]

    def status(self, name: str) -> ThreadStatus:
        self._load()
        name = self._aliases.get(name, name)
        if name not in self._statuses:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._statuses[name]

    def all_threads(self) -> Mapping[str, Thread]:
        self._load()
        return dict(self._threads)

    def _snapshot_unlocked(self) -> RegistrySnapshot:
        """Caller already holds this registry's file lock."""
        self._load_unlocked()
        return RegistrySnapshot(
            dict(self._threads),
            dict(self._statuses),
            dict(self._last_seen),
            dict(self._aliases),
            dict(self._owner_epochs),
            dict(self._turn_epochs),
        )

    def snapshot(self) -> RegistrySnapshot:
        """Read related declarations and statuses from exactly one store revision."""
        with _store_lock(self._path):
            return self._snapshot_unlocked()

    def active_threads(self) -> Mapping[str, Thread]:
        self._load()
        return {name: t for name, t in self._threads.items() if self._statuses[name].active}

    def peers(self, exclude: str) -> Sequence[str]:
        self._load()
        exclude = self._aliases.get(exclude, exclude)
        return [name for name in self._threads if name != exclude]

    def __contains__(self, name: str) -> bool:
        self._load()
        name = self._aliases.get(name, name)
        return name in self._threads


# ─── Message Bus ──────────────────────────────────────────────────────────────
# Persists Message instances as an append-only JSONL log. Delivery is pull-based
# per thread with read markers. At send time, the bus proves the required
# relation: sender and target must resolve to registered threads.


class MessageBus:
    """Routes messages between registered threads."""

    def __init__(
        self,
        bus_path: Path,
        registry: ThreadRegistry,
        *,
        private_response_writes: bool = False,
        private_initial_writes: bool = False,
        private_claim_writes: bool = False,
    ):
        from .channels import ChannelCatalog

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
        self._display_activity_revision: tuple | None = None
        self._display_activity: dict[str, ChannelActivity] = {}
        self._display_activity_verified = False

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
            markers = self._read_markers()
            scopes: tuple[ChannelReadScope | ChannelDisplayScope, ...] = tuple(
                ChannelReadScope(
                    channel,
                    self._channels.history_targets(channel),
                    max(markers.get(viewer, 0), markers.get(self._marker_key(viewer, channel), 0)),
                )
                for channel in self._channels.views()
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

    def display_view_metrics(
        self,
        records: Iterator[tuple[Message, int]],
        scopes: tuple[ChannelDisplayScope, ...],
        activity_scopes: tuple[ChannelDisplayScope, ...],
        viewer: str,
        viewer_names: frozenset[str],
        bus_revision: tuple[int, int, int, int] | None,
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The caller supplies a canonical viewer and alias closure from the same
        captured registry as the scopes. Never recanonicalize during this scan.
        Caches may be reused or published only for an unchanged bus revision.
        """
        activity_key = (bus_revision, activity_scopes)
        unread_key = (bus_revision, viewer_names)
        cached_unread = self._view_unread_cache.get(viewer)
        if (
            bus_revision is not None
            and self._display_activity_revision == activity_key
            and self._display_activity_verified
            and cached_unread is not None
            and cached_unread.revision == unread_key
            and cached_unread.verified_display_boundary
            and cached_unread.scopes == scopes
        ):
            return dict(self._display_activity), dict(cached_unread.counts)

        activity = {scope.channel: ChannelActivity() for scope in activity_scopes}
        counts = dict.fromkeys((scope.channel for scope in scopes), 0)
        for message, _ in records:
            for scope in activity_scopes:
                if scope.includes(message):
                    activity[scope.channel] = activity[scope.channel].observe(message)
            if message.sender in viewer_names:
                continue
            for scope in scopes:
                if scope.unread(message):
                    counts[scope.channel] += 1
        if bus_revision is not None and file_revision(self._path) == bus_revision:
            self._display_activity = activity
            self._display_activity_revision = activity_key
            self._display_activity_verified = True
            self._view_unread_cache[viewer] = ViewUnread(
                unread_key, scopes, counts, verified_display_boundary=True
            )
        return activity, counts

    def mark_view_read(self, viewer: str, target: str, through: int) -> None:
        viewer = self._registry.require(viewer).name
        key = self._marker_key(viewer, self._channels.resolve(target).name)
        if self._read_markers().get(key, 0) < through:
            self._write_markers({key: through})

    def channel_activity(self) -> Mapping[str, ChannelActivity]:
        """Aggregate channel history clocks once per wire revision, not per viewer."""
        with _store_lock(self._path):
            revision = file_revision(self._path)
            if revision != self._channel_activity_revision:
                activity: dict[str, ChannelActivity] = {}
                for message in self._iter_log_unlocked():
                    activity[message.target] = activity.get(
                        message.target, ChannelActivity()
                    ).observe(message)
                self._channel_activity = activity
                self._channel_activity_revision = revision
            return dict(self._channel_activity)

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
        if message.target == BuiltinChannel.ANY.value or self._channels.is_view_target(
            message.target
        ):
            raise RelationViolationError(
                f"View {message.target!r} is a projection, not a routable target."
            )
        if (
            not is_channel_target(message.target)
            and message.target != "broadcast"
            and not exists(message.target)
        ):
            raise UnregisteredThreadError(f"Target {message.target!r} is not a registered thread.")
        sender = canonical(message.sender)
        target = message.target
        if not is_channel_target(target) and target != "broadcast" and canonical(target) == sender:
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
            target=GLOBAL_CHANNEL if target == "broadcast" else target,
            seq=sequence,
            sender_role=snapshot.threads[sender].role,
            mentions=ThreadMention.find(message.body, resolve_mention),
        )

    def publish(self, message: Message) -> Message:
        """Commit an ordinary row; refuse legacy appends after private cutover."""
        if message.claim_transition is not None:
            raise RelationViolationError("Claim envelopes require the gated private sender.")
        sender, target = self._validate_publish_request(message)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            sequence_path = self._path.parent / "bus_meta.json"
            metadata = json.loads(sequence_path.read_text()) if sequence_path.exists() else {}
            if not isinstance(metadata, dict) or "writer_protocol_version" in metadata:
                raise RelationViolationError("Legacy append is unavailable after private cutover.")
            last_sequence = self._read_last_sequence(sequence_path)
            if not last_sequence and self._path.exists():
                last_sequence = self._max_sequence_unlocked()
            stored = self._prepare_message_unlocked(
                message, sender=sender, target=target, sequence=last_sequence + 1
            )
            _atomic_write_text(sequence_path, json.dumps({"last_seq": stored.seq}, indent=2))
            _append_jsonl(self._path, stored.to_wire())
        return stored

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
        """Explicit, default-off marker issuer for a NEW, isolated bus root only.

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
            if self._registry._path.parent != self._path.parent:
                raise RelationViolationError("Private registry must share the bus root")
            with _store_lock(self._registry._path):
                guard = PrivateRegistryGuard(self._registry._path, root_id)
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
        """Default-off gate for a NEW marked root, before ANY bus message exists.

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
        claims: Sequence[str | Path] = (),
        releases: Sequence[str | Path] = (),
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
        with _store_lock(self._path):
            metadata = self._private_marker_unlocked()
            if metadata.get("claim_envelopes_version") != 1:
                raise RelationViolationError("Claim read barrier is unavailable.")
            sender, target = self._validate_publish_request(message)
            projection, verified_sequence = self._claim_projection_unlocked(metadata)
            # Metadata is a reservation hint: its rename can roll back while
            # the existing bus inode retains a separately fsynced append.
            last_sequence = max(int(metadata["last_seq"]), verified_sequence)
            if last_sequence >= MAX_WIRE_SEQ:
                raise RelationViolationError("Claim bus sequence is exhausted.")
            stored = self._prepare_message_unlocked(
                message,
                sender=sender,
                target=target,
                sequence=last_sequence + 1,
            )
            owner_incarnation = str(incarnation)
            requested = tuple(sorted(normalize_existing_file(worktree, path) for path in claims))
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
        self, metadata: Mapping[str, int | str]
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
            while line := stream.readline(8 * 1024 * 1024 + 1):
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
        """Reserve a sequence hint, append one row, then sync the parent.

        A failed parent fsync leaves the outcome UNKNOWN; a later crash may
        retain the fsynced bus append while losing only the marker rename.
        """
        encoded = json.dumps(row, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > 8 * 1024 * 1024:
            raise RelationViolationError("Private bus row exceeds the byte limit.")
        sequence_path = self._path.parent / "bus_meta.json"
        metadata["last_seq"] = row["seq"]  # type: ignore[assignment]
        _atomic_write_text(sequence_path, json.dumps(metadata, indent=2))
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

    def publish_initial_cohort(self, message: Message, *, control: str = "ordinary") -> Message:
        """Commit public envelope and FULL N private decisions in the SAME fsynced row.

        This default-off path assumes cooperating Comms writers hold the global
        wire lock. It never publishes from a caller-supplied audience or claim.
        """
        from .audience_manifest import MAX_WIRE_SEQ, FrozenRecipient, freeze_audience
        from .wake import ControlClassification, resolve_wake_cohort

        if self._private_initial_writes is not True:
            raise RelationViolationError("Private initial publication is disabled.")
        if message.claim_transition is not None:
            raise RelationViolationError("An initial cohort cannot carry resource claims.")
        classification = ControlClassification(control)
        if classification is not ControlClassification.ORDINARY:
            raise RelationViolationError("System-control initial issuer is not available.")
        with _store_lock(self._path):
            metadata = self._private_marker_unlocked()
            previous_sequence = 0
            for previous, _, _ in self._verified_private_rows_unlocked(metadata):
                previous_sequence = previous.seq
            if int(metadata["last_seq"]) >= MAX_WIRE_SEQ:
                raise RelationViolationError("Private bus sequence is exhausted.")
            source_paths = (
                self._registry._path,
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
            if (
                sender not in snapshot.threads
                or not snapshot.threads[sender].role.executable
                or not snapshot.statuses[sender].visible
            ):
                raise RelationViolationError(
                    "Initial sender must be a visible registered executable."
                )
            if message.target == BuiltinChannel.ANY.value or self._channels.is_view_target(
                message.target
            ):
                raise RelationViolationError("A saved/aggregate view is not routable.")
            target = GLOBAL_CHANNEL if message.target == "broadcast" else message.target
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
                if target == "#all" or target == "#none":
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
            revision = hashlib.sha256(
                repr(
                    (
                        file_revision(self._registry._path),
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
            self._append_private_unlocked(metadata, row)
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
        canonical_target = GLOBAL_CHANNEL if target == "broadcast" else target
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
        markers = self._read_markers()
        global_read = markers.get(name, 0)
        with _store_lock(self._path):
            return [
                msg
                for msg in self._iter_log_unlocked()
                if msg.seq > global_read
                and delivery.delivers(msg)
                and matches(msg)
                and msg.seq
                > max(
                    global_read,
                    markers.get(self._marker_key(name, delivery.conversation(msg)), 0),
                )
            ]

    def pending_count(self, name: str, target: str | None = None) -> int:
        """Count unread messages without retaining their bodies."""
        counts = self.pending_counts(name)
        if target is None or target == "#any":
            return sum(counts.values())
        if is_channel_target(target) or target == "broadcast":
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
        if is_channel_target(target) or target == "broadcast":
            targets = self._channels.history_targets(target)
            return lambda message: targets is None or message.target in targets
        peer = self._registry.require(target).name
        return lambda message: delivery.conversation(message) == peer

    def pending_counts(self, name: str) -> Mapping[str, int]:
        """Count one thread's unread messages by conversation in one log pass."""
        revision = tuple(
            file_revision(path)
            for path in (
                self._path,
                self._channels.path,
                self._path.parent / "read_markers.json",
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
            for message in self._iter_log_unlocked():
                if message.seq <= global_read or not delivery.delivers(message):
                    continue
                scope = delivery.conversation(message)
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
        if target != "broadcast" and not is_channel_target(target):
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
                    and delivery.delivers(msg)
                    and matches(msg)
                    and msg.seq
                    > max(
                        global_read,
                        markers.get(self._marker_key(name, delivery.conversation(msg)), 0),
                    )
                ):
                    count += 1
                    latest = msg.seq
                    scoped[self._marker_key(name, delivery.conversation(msg))] = msg.seq
        if not count:
            return 0
        self._write_markers({name: latest} if target is None else scoped)
        return count

    def mark_delivered_through(self, name: str, sequence: int) -> None:
        """Advance a thread's global inbox cursor without loading messages."""
        canonical = self._registry.require(name).name
        if sequence < 0:
            raise ValueError("Delivery sequence cannot be negative.")
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
        if not (is_channel_target(target) or target == "broadcast"):
            raise ValueError(f"{target!r} is not a channel target.")
        targets = self._channels.history_targets(target)
        return [msg for msg in self._load_log() if targets is None or msg.target in targets]

    def incoming_page(self, name: str, *, after: int, limit: int = 100) -> MessagePage:
        """A bounded delivery stream independent of UI read acknowledgments."""
        thread = self._registry.require(name)
        delivery = self._delivery_scope(thread.name)
        return self._history_page(
            delivery.delivers,
            before=None,
            after=after,
            limit=limit,
            max_bytes=256 * 1024,
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
            lambda message: (message.sender in a_names and message.target in b_names)
            or (message.sender in b_names and message.target in a_names),
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
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
        if not (is_channel_target(target) or target == "broadcast"):
            raise ValueError(f"{target!r} is not a channel target.")
        targets = self._channels.history_targets(target)
        return self._history_page(
            lambda message: targets is None or message.target in targets,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
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

    def channel_display_activity(
        self, scopes: tuple[ChannelDisplayScope, ...]
    ) -> Mapping[str, ChannelActivity]:
        """The same display predicate owns activity even without a new wire row."""
        revision = (file_revision(self._path), scopes)
        if revision != self._display_activity_revision:
            activity = {scope.channel: ChannelActivity() for scope in scopes}
            with self._record_snapshot() as (_, records):
                for message, _ in records:
                    for scope in scopes:
                        if scope.includes(message):
                            activity[scope.channel] = activity[scope.channel].observe(message)
            self._display_activity = activity
            self._display_activity_revision = revision
            self._display_activity_verified = False
        return dict(self._display_activity)

    def _history_page(
        self,
        matches: Callable[[Message], bool],
        *,
        before: int | None,
        after: int | None,
        limit: int,
        max_bytes: int,
    ) -> MessagePage:
        with _store_lock(self._path):
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
                    continue
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
        latest: dict[str, float] = {}
        with _store_lock(self._path):
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
        marker_path = self._path.parent / "read_markers.json"
        with _store_lock(marker_path):
            return self._read_markers_unlocked(marker_path)

    @staticmethod
    def _read_markers_unlocked(marker_path: Path) -> dict[str, int]:
        if not marker_path.exists():
            return {}
        markers: dict[str, int] = json.loads(marker_path.read_text())
        return markers

    def _write_markers(self, markers: dict[str, int]) -> None:
        marker_path = self._path.parent / "read_markers.json"
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

    def rename_thread(self, old_name: str, new_name: str) -> None:
        """Move read markers to canonical names without rewriting message history."""
        marker_path = self._path.parent / "read_markers.json"
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
            _atomic_write_text(sequence_path, json.dumps({"last_seq": high_water}, indent=2))
            _atomic_write_text(
                self._path,
                "".join(f"{json.dumps(message.to_wire())}\n" for message in retained),
            )

        marker_path = self._path.parent / "read_markers.json"
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


# ─── Shared Ledger ────────────────────────────────────────────────────────────
# Persists the coordination state (ownership map, stack direction, notes).


class SharedLedger:
    """Persists shared coordination state between threads."""

    def __init__(self, store_path: Path):
        self._path = store_path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        with _store_lock(self._path):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._data = json.loads(self._path.read_text()) if self._path.exists() else {}

    def _save_unlocked(self) -> None:
        _atomic_write_text(self._path, json.dumps(self._data, indent=2))

    def read(self) -> Mapping[str, object]:
        self._load()
        return dict(self._data)

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        for key in updates:
            if not isinstance(key, str):
                raise ValueError(f"Ledger key must be a string, got {type(key).__name__}.")
        with _store_lock(self._path):
            self._load_unlocked()
            self._data.update(updates)
            self._data["last_updated_by"] = author
            self._save_unlocked()

    def remove_thread(self, name: str) -> int:
        """Remove exact structural references to a thread identity."""

        def clean(value: object) -> tuple[object, int]:
            if isinstance(value, dict):
                result: dict[str, object] = {}
                removed = 0
                for key, child in value.items():
                    if key == name or child == name:
                        removed += 1
                        continue
                    cleaned, count = clean(child)
                    result[key] = cleaned
                    removed += count
                return result, removed
            if isinstance(value, list):
                result_list: list[object] = []
                removed = 0
                for child in value:
                    if child == name:
                        removed += 1
                        continue
                    cleaned, count = clean(child)
                    result_list.append(cleaned)
                    removed += count
                return result_list, removed
            return value, 0

        with _store_lock(self._path):
            self._load_unlocked()
            cleaned, removed = clean(self._data)
            self._data = cleaned if isinstance(cleaned, dict) else {}
            self._save_unlocked()
        return removed

    def rename_thread(self, old_name: str, new_name: str) -> int:
        """Replace exact structural references without touching free text."""

        def rename(value: object) -> tuple[object, int]:
            if isinstance(value, dict):
                result: dict[str, object] = {}
                changed = 0
                for key, child in value.items():
                    renamed_child, count = rename(child)
                    renamed_key = new_name if key == old_name else key
                    result[renamed_key] = renamed_child
                    changed += count + (renamed_key != key)
                return result, changed
            if isinstance(value, list):
                result_list: list[object] = []
                changed = 0
                for child in value:
                    renamed_child, count = rename(child)
                    result_list.append(renamed_child)
                    changed += count
                return result_list, changed
            if value == old_name:
                return new_name, 1
            return value, 0

        with _store_lock(self._path):
            self._load_unlocked()
            renamed, changed = rename(self._data)
            self._data = renamed if isinstance(renamed, dict) else {}
            self._save_unlocked()
        return changed


# ─── Runtime thread resolution ────────────────────────────────────────────────


def current_thread() -> Thread:
    """Declare the current thread from process environment.

    Fail-closed: raises if neither PI_AGENT_ID nor AGENT_COMMS_THREAD is set.
    """
    name = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
    if not name:
        raise UnregisteredThreadError(
            "PI_AGENT_ID is not set. This process is not an orchestrated thread."
        )
    tag_env = os.environ.get("PI_AGENT_TAGS") or os.environ.get("AGENT_COMMS_TAGS", "")
    tags = frozenset(t.strip() for t in tag_env.split(",") if t.strip())
    return Thread(
        name=name,
        tags=tags,
        worktree=os.environ.get("PI_WORKTREE", os.getcwd()),
        parent=os.environ.get("PI_PARENT_ID"),
        task=os.environ.get("PI_TASK"),
        pid=os.getpid(),
    )
