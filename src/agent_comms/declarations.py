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
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class UnregisteredThreadError(ValueError):
    """A reference does not resolve to a registered thread."""


class RelationViolationError(ValueError):
    """A required relation between declarations cannot be proved."""


class ThreadStatus(Enum):
    RUNNING = "running"
    IDLE = "idle"
    STOPPED = "stopped"


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

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
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
            )
        )

    def register(self, thread: Thread, status: ThreadStatus = ThreadStatus.RUNNING) -> None:
        self._threads[thread.name] = thread
        self._statuses[thread.name] = status
        self._last_seen[thread.name] = time.time()
        self._save()

    def unregister(self, name: str) -> None:
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        self._statuses[name] = ThreadStatus.STOPPED
        self._save()

    def remove(self, name: str) -> None:
        """Drop the declaration entirely (rollback, not a status change)."""
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        del self._threads[name]
        self._statuses.pop(name, None)
        self._save()

    def heartbeat(self, name: str) -> None:
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        self._statuses[name] = ThreadStatus.RUNNING
        self._last_seen[name] = time.time()
        self._save()

    def last_seen(self, name: str) -> float:
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._last_seen.get(name, 0.0)

    def require(self, name: str) -> Thread:
        if name not in self._threads:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._threads[name]

    def status(self, name: str) -> ThreadStatus:
        if name not in self._statuses:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        return self._statuses[name]

    def all_threads(self) -> Mapping[str, Thread]:
        return dict(self._threads)

    def active_threads(self) -> Mapping[str, Thread]:
        return {
            name: t
            for name, t in self._threads.items()
            if self._statuses.get(name) != ThreadStatus.STOPPED
        }

    def peers(self, exclude: str) -> Sequence[str]:
        return [name for name in self._threads if name != exclude]

    def __contains__(self, name: str) -> bool:
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
        stored = Message(
            sender=message.sender,
            target=GLOBAL_CHANNEL if message.target == "broadcast" else message.target,
            body=message.body,
            type=message.type,
            timestamp=message.timestamp,
            seq=self._next_seq(),
        )
        with open(self._path, "a") as f:
            f.write(json.dumps(stored.to_wire()) + "\n")
        return stored.message_id

    def _next_seq(self) -> int:
        return self.total_messages() + 1

    def _delivered_to(self, message: Message, name: str) -> bool:
        if message.sender == name:
            return False
        target = message.target
        if target == name:
            return True
        if target in BROADCAST_ALIASES:
            return True
        if is_channel_target(target):
            return channel_tag(target) in self._registry.require(name).tags
        return False

    def inbox(self, name: str) -> Sequence[Message]:
        if name not in self._registry:
            raise UnregisteredThreadError(f"Thread {name!r} is not registered.")
        last_read = self._read_markers().get(name, 0)
        return [
            msg for msg in self._load_log() if self._delivered_to(msg, name) and msg.seq > last_read
        ]

    def mark_delivered(self, name: str) -> None:
        inbox = self.inbox(name)
        if not inbox:
            return
        markers = self._read_markers()
        markers[name] = max(msg.seq for msg in inbox)
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
        if not marker_path.exists():
            return {}
        markers: dict[str, int] = json.loads(marker_path.read_text())
        return markers

    def _write_markers(self, markers: dict[str, int]) -> None:
        marker_path = self._path.parent / "read_markers.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(markers, indent=2))

    def _load_log(self) -> list[Message]:
        if not self._path.exists():
            return []
        return [
            Message.from_wire(json.loads(line))
            for line in self._path.read_text().splitlines()
            if line.strip()
        ]


# ─── Shared Ledger ────────────────────────────────────────────────────────────
# Persists the coordination state (ownership map, stack direction, notes).


class SharedLedger:
    """Persists shared coordination state between threads."""

    def __init__(self, store_path: Path):
        self._path = store_path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def read(self) -> Mapping[str, object]:
        return dict(self._data)

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        for key in updates:
            if not isinstance(key, str):
                raise ValueError(f"Ledger key must be a string, got {type(key).__name__}.")
        self._data.update(updates)
        self._data["last_updated_by"] = author
        self._save()


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
