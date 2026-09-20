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

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class UnregisteredThreadError(ValueError):
    """A reference does not resolve to a registered thread."""


class RelationViolationError(ValueError):
    """A required relation between declarations cannot be proved."""


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
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a snapshot without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _repair_trailing_jsonl(path: Path) -> None:
    """Complete a valid unterminated record or quarantine a truncated one."""
    if not path.exists():
        return
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
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


def _jsonl_records(path: Path) -> list[Mapping]:
    """Read complete JSONL records, tolerating only a truncated final record."""
    if not path.exists():
        return []
    lines = path.read_text().splitlines(keepends=True)
    records: list[Mapping] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            is_tail = index == len(lines) - 1 and not raw_line.endswith(("\n", "\r"))
            if is_tail:
                break
            raise
        if not isinstance(record, Mapping):
            raise ValueError(f"JSONL record in {path} must be an object.")
        records.append(record)
    return records


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


class ActivityState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"


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
        if self.detail and self.state is ActivityState.IDLE:
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

    def emit(self, activity: Activity) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            _append_jsonl(self._path, activity.to_wire())

    def current(self, thread: str) -> Activity:
        """Latest activity for one thread; idle when stale or unknown."""
        events = [e for e in self._load() if e.thread == thread]
        if not events:
            return Activity(thread=thread, state=ActivityState.IDLE)
        latest = events[-1]
        if time.time() - latest.timestamp > self._stale_after:
            return Activity(thread=thread, state=ActivityState.IDLE)
        return latest

    def all_current(self) -> dict[str, Activity]:
        """Latest activity per thread (idle included for known threads)."""
        result: dict[str, Activity] = {}
        for event in self._load():
            result[event.thread] = event
        now = time.time()
        return {
            thread: (
                activity
                if now - activity.timestamp <= self._stale_after
                else Activity(thread=thread, state=ActivityState.IDLE)
            )
            for thread, activity in result.items()
        }

    def _load(self) -> list[Activity]:
        with _store_lock(self._path):
            return [Activity.from_wire(record) for record in _jsonl_records(self._path)]


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
class Thread:
    """Declares one agent thread's identity and provenance."""

    name: str
    tags: frozenset[str]
    worktree: str
    parent: str | None = None
    task: str | None = None
    pid: int = 0
    session_file: str | None = None

    def __post_init__(self) -> None:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not self.name or not set(self.name) <= allowed:
            raise ValueError(
                f"Thread name {self.name!r} must be alphanumeric with hyphens/underscores."
            )
        if self.parent == self.name:
            raise RelationViolationError(f"Thread {self.name!r} cannot be its own parent.")
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")

    @property
    def is_fork(self) -> bool:
        return self.parent is not None


# ─── Message ──────────────────────────────────────────────────────────────────
# Declaring a Message proves sender, target, and body are present and distinct.
# The registry proves the required relation (sender and target are registered)
# at send time.


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

    def __post_init__(self) -> None:
        if not self.sender:
            raise RelationViolationError("Message sender cannot be empty.")
        if not self.target:
            raise RelationViolationError("Message target cannot be empty.")
        if not self.body:
            raise ValueError("Message body cannot be empty.")
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
        )


# ─── Thread Registry ──────────────────────────────────────────────────────────
# Persists Thread instances and their statuses. Fail-closed: referencing an
# unregistered thread raises UnregisteredThreadError.


class ThreadRegistry:
    """Persists threads and manages status transitions and presence."""

    def __init__(self, store_path: Path):
        self._path = store_path
        self._threads: dict[str, Thread] = {}
        self._statuses: dict[str, ThreadStatus] = {}
        self._last_seen: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        with _store_lock(self._path):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._threads.clear()
        self._statuses.clear()
        self._last_seen.clear()
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        for name, data in raw.get("threads", {}).items():
            self._threads[name] = Thread(
                name=name,
                tags=frozenset(data.get("tags", [])),
                worktree=data.get("worktree", ""),
                parent=data.get("parent"),
                task=data.get("task"),
                pid=data.get("pid", 0),
                session_file=data.get("session_file"),
            )
            self._statuses[name] = ThreadStatus(data.get("status", "running"))
            self._last_seen[name] = data.get("last_seen", 0.0)

    def _save_unlocked(self) -> None:
        _atomic_write_text(
            self._path,
            json.dumps(
                {
                    "threads": {
                        name: {
                            "tags": sorted(t.tags),
                            "worktree": t.worktree,
                            "parent": t.parent,
                            "task": t.task,
                            "pid": t.pid,
                            "session_file": t.session_file,
                            "status": self._statuses.get(name, ThreadStatus.RUNNING).value,
                            "last_seen": self._last_seen.get(name, 0.0),
                        }
                        for name, t in self._threads.items()
                    }
                },
                indent=2,
            ),
        )

    def register(self, thread: Thread, status: ThreadStatus = ThreadStatus.RUNNING) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            self._threads[thread.name] = thread
            self._statuses[thread.name] = status
            self._last_seen[thread.name] = time.time()
            self._save_unlocked()

    def unregister(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            self._statuses[name] = ThreadStatus.STOPPED
            self._save_unlocked()

    def archive(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            self._statuses[name] = ThreadStatus.ARCHIVED
            self._save_unlocked()

    def remove(self, name: str) -> None:
        """Drop the declaration entirely (rollback, not a status change)."""
        with _store_lock(self._path):
            self._load_unlocked()
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            del self._threads[name]
            self._statuses.pop(name, None)
            self._last_seen.pop(name, None)
            self._save_unlocked()

    def heartbeat(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            self._statuses[name] = ThreadStatus.RUNNING
            self._last_seen[name] = time.time()
            self._save_unlocked()

    def last_seen(self, name: str) -> float:
        self._load()
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._last_seen.get(name, 0.0)

    def require(self, name: str) -> Thread:
        self._load()
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._threads[name]

    def status(self, name: str) -> ThreadStatus:
        self._load()
        if name not in self._statuses:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._statuses[name]

    def all_threads(self) -> Mapping[str, Thread]:
        self._load()
        return dict(self._threads)

    def active_threads(self) -> Mapping[str, Thread]:
        self._load()
        return {
            name: t
            for name, t in self._threads.items()
            if self._statuses.get(name) not in {ThreadStatus.STOPPED, ThreadStatus.ARCHIVED}
        }

    def peers(self, exclude: str) -> Sequence[str]:
        self._load()
        return [name for name in self._threads if name != exclude]

    def __contains__(self, name: str) -> bool:
        self._load()
        return name in self._threads


# ─── Message Bus ──────────────────────────────────────────────────────────────
# Persists Message instances as an append-only JSONL log. Delivery is pull-based
# per thread with read markers. At send time, the bus proves the required
# relation: sender and target must resolve to registered threads.


class MessageBus:
    """Routes messages between registered threads."""

    def __init__(self, bus_path: Path, registry: ThreadRegistry):
        self._path = bus_path
        self._registry = registry

    def send(self, message: Message) -> str:
        if message.sender not in self._registry:
            raise UnregisteredThreadError(f"Sender {message.sender!r} is not a registered thread.")
        if (
            not is_channel_target(message.target)
            and message.target != "broadcast"
            and message.target not in self._registry
        ):
            raise UnregisteredThreadError(f"Target {message.target!r} is not a registered thread.")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            messages = self._load_log_unlocked()
            stored = Message(
                sender=message.sender,
                target=GLOBAL_CHANNEL if message.target == "broadcast" else message.target,
                body=message.body,
                type=message.type,
                timestamp=message.timestamp,
                seq=max((existing.seq for existing in messages), default=0) + 1,
            )
            _append_jsonl(self._path, stored.to_wire())
        return stored.message_id

    def _next_seq(self) -> int:
        messages = self._load_log()
        return max((message.seq for message in messages), default=0) + 1

    def _delivered_to(self, message: Message, name: str, tags: frozenset[str]) -> bool:
        if message.sender == name:
            return False
        target = message.target
        if target == name:
            return True
        if target in BROADCAST_ALIASES:
            return True
        if is_channel_target(target):
            return channel_tag(target) in tags
        return False

    @staticmethod
    def _marker_key(name: str, target: str) -> str:
        return json.dumps([name, target], separators=(",", ":"))

    @staticmethod
    def _in_scope(message: Message, name: str, target: str) -> bool:
        normalized = GLOBAL_CHANNEL if target == "broadcast" else target
        if is_channel_target(normalized):
            return message.target == normalized
        return {message.sender, message.target} == {name, target}

    @staticmethod
    def _message_scope(message: Message, name: str) -> str:
        if is_channel_target(message.target):
            return message.target
        return message.sender if message.target == name else message.target

    def inbox(self, name: str, target: str | None = None) -> Sequence[Message]:
        thread = self._registry.require(name)
        if target is not None and not is_channel_target(target) and target != "broadcast":
            self._registry.require(target)
        markers = self._read_markers()
        global_read = markers.get(name, 0)
        return [
            msg
            for msg in self._load_log()
            if self._delivered_to(msg, name, thread.tags)
            and (target is None or self._in_scope(msg, name, target))
            and msg.seq
            > max(
                global_read,
                markers.get(self._marker_key(name, self._message_scope(msg, name)), 0),
            )
        ]

    def mark_delivered(self, name: str, target: str | None = None) -> None:
        inbox = self.inbox(name, target)
        if not inbox:
            return
        markers = self._read_markers()
        key = name if target is None else self._marker_key(name, target)
        markers[key] = max(msg.seq for msg in inbox)
        self._write_markers(markers)

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, in seq order."""
        self._registry.require(a)
        self._registry.require(b)
        return [msg for msg in self._load_log() if {msg.sender, msg.target} == {a, b}]

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full history of one channel (``#all`` or a tag channel)."""
        if not (is_channel_target(target) or target == "broadcast"):
            raise ValueError(f"{target!r} is not a channel target.")
        normalized = GLOBAL_CHANNEL if target == "broadcast" else target
        return [msg for msg in self._load_log() if msg.target == normalized]

    def full_history(self) -> Sequence[Message]:
        """Every message on the wire, in seq order (the combined view)."""
        return self._load_log()

    def channels(self) -> Sequence[str]:
        """Derived channel list: the global channel plus one per tag in use."""
        tags: set[str] = set()
        for thread in self._registry.all_threads().values():
            tags |= thread.tags
        return [GLOBAL_CHANNEL] + sorted(f"#{tag}" for tag in tags)

    def total_messages(self) -> int:
        return len(self._load_log())

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
        return [Message.from_wire(record) for record in _jsonl_records(self._path)]


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
