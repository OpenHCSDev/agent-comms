"""OpenHCS agent communications — operations layer.

Business logic on top of the declaration-owned core. Every UI adapter
(Toad via ACP, VS Code, pi extension, CLI) calls these operations; none of
them own orchestration semantics.

Fail-closed throughout: every operation proves required relations before
acting, and raises on unregistered references.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .declarations import (
    Activity,
    ActivityLog,
    ActivityState,
    AgentRuntimeInfo,
    Message,
    MessageBus,
    MessageType,
    RelationViolationError,
    RuntimeInfoStore,
    SharedLedger,
    Thread,
    ThreadRegistry,
    ThreadStatus,
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
        self.activity = ActivityLog(self.root / "activity.jsonl")
        self.runtime_info = RuntimeInfoStore(self.root / "runtime_info.json")

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

    def inbox(self, name: str, target: str | None = None) -> Sequence[Message]:
        """Undelivered messages for one thread, optionally scoped to a conversation."""
        return self.bus.inbox(name, target)

    def acknowledge(self, name: str, target: str | None = None) -> int:
        """Mark an inbox or one conversation delivered. Returns count acknowledged."""
        messages = self.inbox(name, target)
        self.bus.mark_delivered(name, target)
        return len(messages)

    def pending_count(self, name: str, target: str | None = None) -> int:
        return len(self.inbox(name, target))

    # ─── IRC views ────────────────────────────────────────────────────────────

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, in seq order."""
        return self.bus.dm_history(a, b)

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full history of one channel (``#all`` or a tag channel)."""
        return self.bus.channel_history(target)

    def full_history(self) -> Sequence[Message]:
        """Every message on the wire, in seq order (the combined view)."""
        return self.bus.full_history()

    # ─── Activity (live feedback) ─────────────────────────────────────────────

    def set_activity(self, thread: str, state: ActivityState, detail: str = "") -> None:
        """Declare a thread's current activity (thinking/working/idle)."""
        self.registry.require(thread)
        self.activity.emit(Activity(thread=thread, state=state, detail=detail))

    def activity_of(self, thread: str) -> Activity:
        return self.activity.current(thread)

    def all_activity(self) -> Mapping[str, Activity]:
        return self.activity.all_current()

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
        self.registry.require(thread)
        self.runtime_info.set(
            AgentRuntimeInfo(
                thread=thread,
                model=model,
                session_name=session_name,
                context_used=context_used,
                context_size=context_size,
            )
        )

    def agent_info_of(self, thread: str) -> AgentRuntimeInfo | None:
        self.registry.require(thread)
        return self.runtime_info.get(thread)

    def all_agent_info(self) -> Mapping[str, AgentRuntimeInfo]:
        return self.runtime_info.all()

    def channels(self) -> Sequence[str]:
        """Derived channel list: ``#all`` plus one channel per tag in use."""
        return self.bus.channels()

    def who(self) -> Sequence[Mapping]:
        """Presence: who is in the chat, with status and unread counts."""
        rows = []
        runtime_info = self.runtime_info.all()
        for name, t in sorted(self.registry.all_threads().items()):
            if self.registry.status(name) is ThreadStatus.ARCHIVED:
                continue
            info = runtime_info.get(name)
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
                    "model": info.model if info else None,
                    "session_name": info.session_name if info else None,
                    "context_used": info.context_used if info else None,
                    "context_size": info.context_size if info else None,
                    "context_percent": info.context_percent if info else None,
                }
            )
        return rows

    # ─── Threads ──────────────────────────────────────────────────────────────

    def register(self, thread: Thread) -> None:
        """Declare a thread in the registry.

        Re-declaring an existing thread cannot silently drop provenance:
        empty tags inherit the prior declaration's tags (a child that does
        not receive tag env still keeps its channel subscriptions), and a
        missing session_file keeps the prior one. Explicit values always win.
        """
        existing = self.registry.all_threads().get(thread.name)
        tags = thread.tags
        session_file = thread.session_file
        if existing is not None:
            if not tags:
                tags = existing.tags
            if session_file is None:
                session_file = existing.session_file
        if tags != thread.tags or session_file != thread.session_file:
            thread = Thread(
                name=thread.name,
                tags=tags,
                worktree=thread.worktree,
                parent=thread.parent,
                task=thread.task,
                pid=thread.pid,
                session_file=session_file,
            )
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
        """Stop a registered participant and retain it in history."""
        thread = self.registry.require(name)
        if self.registry.status(name) in {ThreadStatus.STOPPED, ThreadStatus.ARCHIVED}:
            return
        if thread.pid <= 0 or thread.pid == os.getpid():
            self.registry.unregister(name)
            return

        if not self._is_local_participant(thread):
            raise RelationViolationError(
                f"Refusing to signal unverifiable process {thread.pid} for {name!r}."
            )
        try:
            if os.getpgid(thread.pid) == thread.pid:
                os.killpg(thread.pid, signal.SIGTERM)
            else:
                os.kill(thread.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.registry.unregister(name)
            return

        deadline = time.monotonic() + 3.0
        while self._process_alive(thread.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._process_alive(thread.pid):
            if os.getpgid(thread.pid) == thread.pid:
                os.killpg(thread.pid, signal.SIGKILL)
            else:
                os.kill(thread.pid, signal.SIGKILL)
            deadline = time.monotonic() + 1.0
            while self._process_alive(thread.pid) and time.monotonic() < deadline:
                time.sleep(0.05)
        if self._process_alive(thread.pid):
            raise RuntimeError(f"Process {thread.pid} did not stop.")
        self.registry.unregister(name)

    @staticmethod
    def _process_alive(pid: int) -> bool:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().split()
            if len(stat) > 2 and stat[2] == "Z":
                return False
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def _is_local_participant(self, thread: Thread) -> bool:
        """Prove a PID belongs to the named participant before signaling it."""
        if not sys.platform.startswith("linux"):
            return False
        try:
            entries = Path(f"/proc/{thread.pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return False
        environ = dict(item.split(b"=", 1) for item in entries if item and b"=" in item)
        expected = thread.name.encode()
        name_matches = any(
            item in {b"AGENT_COMMS_THREAD=" + expected, b"PI_AGENT_ID=" + expected}
            for item in entries
        )
        root = Path(environ.get(b"AGENT_COMMS_ROOT", b"~/.agent-comms").decode()).expanduser()
        return name_matches and root.resolve() == self.root.resolve()

    def archive(self, name: str) -> None:
        """Hide a stopped participant from presence while retaining messages."""
        if self.registry.status(name) is not ThreadStatus.STOPPED:
            raise RelationViolationError("Stop a running thread before archiving it.")
        self.registry.archive(name)
        self.runtime_info.remove(name)

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
        tag_env = ",".join(sorted(spec.tags))
        env.update(
            {
                "PI_AGENT_ID": spec.name,
                "PI_PARENT_ID": spec.parent,
                "PI_TASK": spec.task,
                "PI_AGENT_TAGS": tag_env,
                # Non-pi harnesses read the neutral names.
                "AGENT_COMMS_THREAD": spec.name,
                "AGENT_COMMS_TAGS": tag_env,
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
