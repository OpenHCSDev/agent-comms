"""OpenHCS agent communications — operations layer.

Business logic on top of the declaration-owned core. Every UI adapter
(Toad via ACP, VS Code, pi extension, CLI) calls these operations; none of
them own orchestration semantics.

Fail-closed throughout: every operation proves required relations before
acting, and raises on unregistered references.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .declarations import (
    Message,
    MessageBus,
    MessageType,
    RelationViolationError,
    SharedLedger,
    Thread,
    ThreadRegistry,
    UnregisteredThreadError,
    current_thread,
)


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


class Comms:
    """Wire of registry, bus, and ledger rooted at one directory."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.registry = ThreadRegistry(self.root / "registry.json")
        self.bus = MessageBus(self.root / "bus.jsonl", self.registry)
        self.ledger = SharedLedger(self.root / "ledger.json")

    # ─── Messaging ────────────────────────────────────────────────────────────

    def send(
        self,
        sender: str,
        target: str,
        body: str,
        type: MessageType = MessageType.INFO,
    ) -> str:
        """Declare a message and route it through the bus."""
        if sender not in self.registry:
            raise UnregisteredThreadError(f"Sender {sender!r} is not registered.")
        return self.bus.send(Message(sender=sender, target=target, body=body, type=type))

    def broadcast(self, sender: str, body: str) -> str:
        """Declare a message addressed to every peer."""
        return self.send(sender, "broadcast", body)

    def inbox(self, name: str) -> Sequence[Message]:
        """Undelivered messages for one thread."""
        return self.bus.inbox(name)

    def acknowledge(self, name: str) -> int:
        """Mark inbox delivered. Returns count acknowledged."""
        messages = self.inbox(name)
        self.bus.mark_delivered(name)
        return len(messages)

    def pending_count(self, name: str) -> int:
        return len(self.inbox(name))

    # ─── IRC views ────────────────────────────────────────────────────────────

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, in seq order."""
        return self.bus.dm_history(a, b)

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full history of one channel (``#all`` or a tag channel)."""
        return self.bus.channel_history(target)

    def channels(self) -> Sequence[str]:
        """Derived channel list: ``#all`` plus one channel per tag in use."""
        return self.bus.channels()

    def who(self) -> Sequence[Mapping]:
        """Presence: who is in the chat, with status and unread counts."""
        rows = []
        for name, t in sorted(self.registry.all_threads().items()):
            rows.append(
                {
                    "name": name,
                    "status": self.registry.status(name).value,
                    "tags": sorted(t.tags),
                    "task": t.task,
                    "parent": t.parent,
                    "worktree": t.worktree,
                    "last_seen": self.registry.last_seen(name),
                    "pending": self.pending_count(name),
                }
            )
        return rows

    # ─── Threads ──────────────────────────────────────────────────────────────

    def register(self, thread: Thread) -> None:
        """Declare a thread in the registry."""
        self.registry.register(thread)

    def list_threads(self, active_only: bool = False) -> Sequence[Mapping]:
        """Summarize threads with status and pending counts."""
        threads = self.registry.active_threads() if active_only else self.registry.all_threads()
        return [
            {
                "name": name,
                "status": self.registry.status(name).value,
                "parent": t.parent,
                "task": t.task,
                "tags": sorted(t.tags),
                "worktree": t.worktree,
                "pending": self.pending_count(name),
            }
            for name, t in sorted(threads.items())
        ]

    def thread_detail(self, name: str) -> Mapping:
        t = self.registry.require(name)
        return {
            "name": t.name,
            "status": self.registry.status(name).value,
            "parent": t.parent,
            "task": t.task,
            "tags": sorted(t.tags),
            "worktree": t.worktree,
            "pid": t.pid,
            "session_file": t.session_file,
            "pending": self.pending_count(name),
            "is_fork": t.is_fork,
        }

    def heartbeat(self, name: str) -> None:
        self.registry.heartbeat(name)

    def stop(self, name: str) -> None:
        self.registry.unregister(name)

    # ─── Forking ──────────────────────────────────────────────────────────────

    def fork(self, spec: ForkSpec, pi_bin: str = "pi") -> Thread:
        """Spawn a child pi thread from the parent's session.

        Proves the parent is registered and has a session file, declares the
        child thread, registers it, then launches the subprocess. Fail-closed:
        if the launch fails the registration is rolled back.
        """
        parent = self.registry.require(spec.parent)
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
        )
        self.registry.register(child)

        env = os.environ.copy()
        env.update(
            {
                "PI_AGENT_ID": spec.name,
                "PI_PARENT_ID": spec.parent,
                "PI_TASK": spec.task,
                "PI_AGENT_TAGS": ",".join(sorted(spec.tags)),
                "PI_WORKTREE": parent.worktree,
            }
        )

        args = [
            pi_bin,
            "--fork",
            parent.session_file,
            "-p",
            spec.prompt or spec.task,
        ]
        try:
            proc = subprocess.Popen(
                args,
                env=env,
                cwd=parent.worktree,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self.registry.remove(spec.name)
            raise

        launched = Thread(
            name=child.name,
            tags=child.tags,
            worktree=child.worktree,
            parent=child.parent,
            task=child.task,
            pid=proc.pid,
        )
        self.registry.register(launched)
        return launched

    # ─── Ledger ───────────────────────────────────────────────────────────────

    def ledger_read(self) -> Mapping[str, object]:
        return self.ledger.read()

    def ledger_merge(self, updates: Mapping[str, object], author: str) -> None:
        if author not in self.registry:
            raise UnregisteredThreadError(f"Author {author!r} is not registered.")
        self.ledger.merge(updates, author)

    # ─── Runtime ──────────────────────────────────────────────────────────────

    def adopt_current(self) -> Thread:
        """Declare and register the current process's thread from env."""
        thread = current_thread()
        self.registry.register(thread)
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
            "peers": list(self.registry.peers(name)),
        }


def wire(root: Path | str | None = None) -> Comms:
    """Build a Comms wire. Defaults to ~/.agent-comms or $AGENT_COMMS_ROOT."""
    if root is None:
        root = os.environ.get("AGENT_COMMS_ROOT", "~/.agent-comms")
    return Comms(Path(root).expanduser())
