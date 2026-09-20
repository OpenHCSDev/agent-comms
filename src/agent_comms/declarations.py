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
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
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


def _iter_jsonl_records(path: Path) -> Iterator[tuple[Mapping, int]]:
    """Yield JSONL records and encoded sizes without materializing the log."""
    if not path.exists():
        return
    with open(path, "rb") as records:
        for raw_line in records:
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
                raise ValueError(f"JSONL record in {path} must be an object.")
            yield record, len(raw_line)


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
        self._aliases: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        with _store_lock(self._path):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._threads.clear()
        self._statuses.clear()
        self._last_seen.clear()
        self._aliases.clear()
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
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
                    },
                    "aliases": dict(sorted(self._aliases.items())),
                },
                indent=2,
            ),
        )

    def register(self, thread: Thread, status: ThreadStatus = ThreadStatus.RUNNING) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            if thread.name in self._aliases:
                raise RelationViolationError(
                    f"Thread name {thread.name!r} is a permanent alias and cannot be reused."
                )
            if self._statuses.get(thread.name) is ThreadStatus.DELETING:
                raise RelationViolationError(
                    f"Thread {thread.name!r} is being permanently deleted."
                )
            self._threads[thread.name] = thread
            self._statuses[thread.name] = status
            self._last_seen[thread.name] = time.time()
            self._save_unlocked()

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
            if self._statuses[canonical] is not ThreadStatus.RUNNING:
                raise RelationViolationError("Only a running thread can rename itself.")
            if new_name in self._threads or new_name in self._aliases:
                raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
            # Constructing the replacement proves the new name is valid.
            replacement = Thread(
                name=new_name,
                tags=current.tags,
                worktree=current.worktree,
                parent=current.parent,
                task=current.task,
                pid=current.pid,
                session_file=current.session_file,
            )
            status = self._statuses.pop(canonical)
            last_seen = self._last_seen.pop(canonical)
            del self._threads[canonical]
            self._threads[new_name] = replacement
            self._statuses[new_name] = status
            self._last_seen[new_name] = last_seen

            for child_name, child in tuple(self._threads.items()):
                if child.parent == canonical:
                    self._threads[child_name] = Thread(
                        name=child.name,
                        tags=child.tags,
                        worktree=child.worktree,
                        parent=new_name,
                        task=child.task,
                        pid=child.pid,
                        session_file=child.session_file,
                    )
            for alias, target in tuple(self._aliases.items()):
                if target == canonical:
                    self._aliases[alias] = new_name
            self._aliases[canonical] = new_name
            self._save_unlocked()
            return canonical, new_name

    def unregister(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            self._statuses[name] = ThreadStatus.STOPPED
            self._save_unlocked()

    def archive(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            self._statuses[name] = ThreadStatus.ARCHIVED
            self._save_unlocked()

    def begin_delete(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            status = self._statuses.get(name)
            if status not in {
                ThreadStatus.STOPPED,
                ThreadStatus.ARCHIVED,
                ThreadStatus.DELETING,
            }:
                raise RelationViolationError(
                    "Stop a running thread before permanently deleting it."
                )
            self._statuses[name] = ThreadStatus.DELETING
            self._save_unlocked()

    def remove(self, name: str) -> None:
        """Drop the declaration entirely (rollback, not a status change)."""
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            del self._threads[name]
            self._statuses.pop(name, None)
            self._last_seen.pop(name, None)
            if name in self._aliases.values():
                self._aliases[name] = name
            self._save_unlocked()

    def heartbeat(self, name: str) -> None:
        with _store_lock(self._path):
            self._load_unlocked()
            name = self._aliases.get(name, name)
            if name not in self._threads:
                raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
            if self._statuses.get(name) is ThreadStatus.DELETING:
                raise RelationViolationError(f"Thread {name!r} is being permanently deleted.")
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

    def active_threads(self) -> Mapping[str, Thread]:
        self._load()
        return {
            name: t
            for name, t in self._threads.items()
            if self._statuses.get(name)
            not in {ThreadStatus.STOPPED, ThreadStatus.ARCHIVED, ThreadStatus.DELETING}
        }

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
        sender = self._registry.canonical_name(message.sender)
        target = message.target
        if (
            not is_channel_target(target)
            and target != "broadcast"
            and self._registry.canonical_name(target) == sender
        ):
            raise RelationViolationError(f"Thread {sender!r} cannot message itself.")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            sequence_path = self._path.parent / "bus_meta.json"
            last_sequence = self._read_last_sequence(sequence_path)
            if not last_sequence and self._path.exists():
                last_sequence = self._max_sequence_unlocked()
            stored = Message(
                sender=sender,
                target=GLOBAL_CHANNEL if target == "broadcast" else target,
                body=message.body,
                type=message.type,
                timestamp=message.timestamp,
                seq=last_sequence + 1,
            )
            _atomic_write_text(sequence_path, json.dumps({"last_seq": stored.seq}, indent=2))
            _append_jsonl(self._path, stored.to_wire())
        return stored.message_id

    def _next_seq(self) -> int:
        return self.latest_sequence() + 1

    def _delivered_to(self, message: Message, name: str, tags: frozenset[str]) -> bool:
        if self._registry.canonical_name(message.sender) == name:
            return False
        target = message.target
        if not is_channel_target(target) and self._registry.canonical_name(target) == name:
            return True
        if target in BROADCAST_ALIASES:
            return True
        if is_channel_target(target):
            return channel_tag(target) in tags
        return False

    @staticmethod
    def _marker_key(name: str, target: str) -> str:
        return json.dumps([name, target], separators=(",", ":"))

    def _in_scope(self, message: Message, name: str, target: str) -> bool:
        normalized = GLOBAL_CHANNEL if target == "broadcast" else target
        if is_channel_target(normalized):
            return message.target == normalized
        return {
            self._registry.canonical_name(message.sender),
            self._registry.canonical_name(message.target),
        } == {name, target}

    def _message_scope(self, message: Message, name: str) -> str:
        if is_channel_target(message.target):
            return message.target
        sender = self._registry.canonical_name(message.sender)
        target = self._registry.canonical_name(message.target)
        return sender if target == name else target

    def inbox(self, name: str, target: str | None = None) -> Sequence[Message]:
        thread = self._registry.require(name)
        name = thread.name
        if target is not None and not is_channel_target(target) and target != "broadcast":
            target = self._registry.require(target).name
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
        normalized = GLOBAL_CHANNEL if target == "broadcast" else target
        return [msg for msg in self._load_log() if msg.target == normalized]

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
        normalized = GLOBAL_CHANNEL if target == "broadcast" else target
        return self._history_page(
            lambda message: message.target == normalized,
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
        has_older = False
        has_newer = False
        with _store_lock(self._path):
            for record, encoded_size in _iter_jsonl_records(self._path):
                message = Message.from_wire(record)
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

    def channels(self) -> Sequence[str]:
        """Derived channel list: the global channel plus one per tag in use."""
        tags: set[str] = set()
        for thread in self._registry.all_threads().values():
            tags |= thread.tags
        return [GLOBAL_CHANNEL] + sorted(f"#{tag}" for tag in tags)

    def total_messages(self) -> int:
        with _store_lock(self._path):
            return sum(1 for _ in _iter_jsonl_records(self._path))

    def latest_sequence(self) -> int:
        """Return the global high-water sequence without loading message bodies."""
        sequence_path = self._path.parent / "bus_meta.json"
        with _store_lock(self._path):
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
        return [Message.from_wire(record) for record in _jsonl_records(self._path)]

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

    def remove_thread(self, name: str) -> tuple[int, int]:
        """Purge messages and read markers owned by or targeting a thread."""
        names = self._registry.aliases_for(name)
        with _store_lock(self._path):
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
