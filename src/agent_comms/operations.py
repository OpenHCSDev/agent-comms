"""OpenHCS agent communications — operations layer.

Business logic on top of the declaration-owned core. Every UI adapter
(Toad via ACP, VS Code, pi extension, CLI) calls these operations; none of
them own orchestration semantics.

Fail-closed throughout: every operation proves required relations before
acting, and raises on unregistered references.
"""

from __future__ import annotations

import json
import os
import re
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
    MessagePage,
    MessageType,
    RelationViolationError,
    RuntimeInfoStore,
    SharedLedger,
    Thread,
    ThreadRegistry,
    ThreadStatus,
    UnregisteredThreadError,
    _store_lock,
    current_thread,
)


def _session_model(session_file: Path) -> tuple[str, str] | None:
    """Read the last model choice from a pi session file.

    Non-interactive pi requires an explicit provider/model, so forks replay the
    parent's final ``model_change`` entry. Returns ``(provider, model_id)`` or
    ``None`` when the session carries no model record.
    """
    try:
        lines = session_file.read_text().splitlines()
    except OSError:
        return None
    model: tuple[str, str] | None = None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "model_change":
            provider = entry.get("provider")
            model_id = entry.get("modelId")
            if provider and model_id:
                model = (str(provider), str(model_id))
    return model


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
class TranscriptEvent:
    """One normalized event from a thread's persisted Pi transcript."""

    kind: str
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    raw_input: object | None = None
    ok: bool = True


class Comms:
    """Wire of registry, bus, and ledger rooted at one directory."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.registry = ThreadRegistry(self.root / "registry.json")
        self.bus = MessageBus(self.root / "bus.jsonl", self.registry)
        self.ledger = SharedLedger(self.root / "ledger.json")
        self.activity = ActivityLog(self.root / "activity.jsonl")
        self.runtime_info = RuntimeInfoStore(self.root / "runtime_info.json")
        self._wire_lock_path = self.root / "wire"

    # ─── Messaging ────────────────────────────────────────────────────────────

    def send(
        self,
        sender: str,
        target: str,
        body: str,
        type: MessageType = MessageType.INFO,
    ) -> str:
        """Declare a message and route it through the bus."""
        with _store_lock(self._wire_lock_path):
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

    def message_high_water(self) -> int:
        """Global message cursor used by polling clients to avoid idle scans."""
        return self.bus.latest_sequence()

    def full_history(self) -> Sequence[Message]:
        """Every message on the wire, in seq order (the combined view)."""
        return self.bus.full_history()

    # ─── Activity (live feedback) ─────────────────────────────────────────────

    def set_activity(self, thread: str, state: ActivityState, detail: str = "") -> None:
        """Declare a thread's current activity (thinking/working/idle)."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(thread).name
            self.activity.emit(Activity(thread=canonical, state=state, detail=detail))

    def activity_of(self, thread: str) -> Activity:
        return self.activity.current(self.registry.require(thread).name)

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
        """Derived channel list: ``#all`` plus one channel per tag in use."""
        return self.bus.channels()

    def who(self) -> Sequence[Mapping]:
        """Presence: who is in the chat, with status and unread counts."""
        return self._presence(include_pending=True)

    def presence(self) -> Sequence[Mapping]:
        """Presence without viewer-specific unread scans."""
        return self._presence(include_pending=False)

    def _presence(self, *, include_pending: bool) -> Sequence[Mapping]:
        rows = []
        runtime_info = self.runtime_info.all()
        activities = self.activity.all_current()
        for name, t in sorted(self.registry.all_threads().items()):
            if self.registry.status(name) in {
                ThreadStatus.ARCHIVED,
                ThreadStatus.DELETING,
            }:
                continue
            info = runtime_info.get(name)
            activity = activities.get(name)
            row = {
                "name": name,
                "status": self.registry.status(name).value,
                "tags": sorted(t.tags),
                "task": t.task,
                "parent": t.parent,
                "worktree": t.worktree,
                "last_seen": self.registry.last_seen(name),
                "model": info.model if info else None,
                "session_name": info.session_name if info else None,
                "context_used": info.context_used if info else None,
                "context_size": info.context_size if info else None,
                "context_percent": info.context_percent if info else None,
                "resumable": bool(t.session_file),
                "activity": activity.state.value if activity else ActivityState.IDLE.value,
                "activity_detail": activity.detail if activity else "",
            }
            if include_pending:
                row["pending"] = self.pending_count(name)
            rows.append(row)
        return rows

    def thread_transcript(
        self,
        name: str,
        *,
        max_messages: int = 500,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> Sequence[TranscriptEvent]:
        """Return a bounded normalized tail of one thread's Pi session transcript."""
        thread = self.registry.require(name)
        if not thread.session_file or max_messages <= 0 or max_bytes <= 0:
            return ()
        path = Path(thread.session_file)
        try:
            size = path.stat().st_size
            start = max(0, size - max_bytes)
            with path.open("rb") as transcript:
                transcript.seek(start)
                if start:
                    transcript.readline()
                lines = transcript.readlines()
        except OSError:
            return ()

        records: list[list[TranscriptEvent]] = []
        for raw_line in lines:
            try:
                payload = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if payload.get("type") != "message" or not isinstance(
                message := payload.get("message"), dict
            ):
                continue
            events = self._transcript_message_events(message)
            if events:
                records.append(events)

        truncated = start > 0 or len(records) > max_messages
        records = records[-max_messages:]
        events = [event for record in records for event in record]
        if truncated:
            events.insert(
                0,
                TranscriptEvent(
                    "notice",
                    "Earlier transcript content was omitted from this bounded view.",
                ),
            )
        return tuple(events)

    @staticmethod
    def _transcript_message_events(message: Mapping[str, object]) -> list[TranscriptEvent]:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            parts: Sequence[object] = ({"type": "text", "text": content},)
        elif isinstance(content, list):
            parts = content
        else:
            return []

        events: list[TranscriptEvent] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if role == "user" and kind == "text":
                events.append(TranscriptEvent("user", str(part.get("text") or "")))
            elif role == "assistant" and kind == "thinking":
                events.append(TranscriptEvent("thinking", str(part.get("thinking") or "")))
            elif role == "assistant" and kind == "text":
                events.append(TranscriptEvent("assistant", str(part.get("text") or "")))
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
                return [
                    TranscriptEvent(
                        "tool_end",
                        text=output[:16_000],
                        tool_call_id=str(message.get("toolCallId") or ""),
                        tool_name=str(message.get("toolName") or "tool"),
                        ok=not bool(message.get("isError")),
                    )
                ]
        return events

    # ─── Threads ──────────────────────────────────────────────────────────────

    def register(self, thread: Thread) -> None:
        """Declare a thread in the registry.

        Re-declaring an existing thread cannot silently drop provenance:
        empty tags inherit the prior declaration's tags (a child that does
        not receive tag env still keeps its channel subscriptions), and a
        missing session_file keeps the prior one. Explicit values always win.
        """
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.canonical_name(thread.name)
            existing = self.registry.all_threads().get(canonical)
            tags = thread.tags
            session_file = thread.session_file
            if existing is not None:
                if not tags:
                    tags = existing.tags
                if session_file is None:
                    session_file = existing.session_file
            if (
                canonical != thread.name
                or tags != thread.tags
                or session_file != thread.session_file
            ):
                thread = Thread(
                    name=canonical,
                    tags=tags,
                    worktree=thread.worktree,
                    parent=thread.parent,
                    task=thread.task,
                    pid=thread.pid,
                    session_file=session_file,
                )
            self.registry.register(thread)

    def claim_thread(
        self,
        base_name: str,
        *,
        tags: frozenset[str],
        worktree: str,
        pid: int = 0,
        start_at_latest: bool = False,
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
            )
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
            return self._rename_thread(caller, new_name)

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
                    return RenameThreadResult(thread.name, thread.name, False)
                new_name = f"{base_name}-{suffix}"
                suffix += 1
            return self._rename_thread(thread.name, new_name)

    def _rename_thread(self, name: str, new_name: str) -> RenameThreadResult:
        previous, current = self.registry.rename(name, new_name)
        if previous == current:
            return RenameThreadResult(previous, current, False)
        self.bus.rename_thread(previous, current)
        self.activity.rename_thread(previous, current)
        self.runtime_info.rename_thread(previous, current)
        self.ledger.rename_thread(previous, current)
        return RenameThreadResult(previous, current, True)

    def list_threads(self, active_only: bool = False) -> Sequence[Mapping]:
        """Summarize threads with status and pending counts."""
        threads = self.registry.active_threads() if active_only else self.registry.all_threads()
        activities = self.activity.all_current()
        return [
            {
                "name": name,
                "status": self.registry.status(name).value,
                "parent": t.parent,
                "task": t.task,
                "tags": sorted(t.tags),
                "worktree": t.worktree,
                "pending": self.pending_count(name),
                "activity": activities[name].state.value if name in activities else "idle",
                "activity_detail": activities[name].detail if name in activities else "",
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
        with _store_lock(self._wire_lock_path):
            self.registry.heartbeat(name)

    def acquire_thread(self, name: str, *, owner_pid: int) -> Thread:
        """Claim an offline thread, or return its existing live owner unchanged."""
        from dataclasses import replace

        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            if thread.pid > 0 and thread.pid != owner_pid and self._process_alive(thread.pid):
                return thread
            owned = replace(thread, pid=owner_pid)
            self.registry.register(owned)
            return owned

    def attach_session(self, name: str, session_file: str, *, pid: int | None = None) -> Thread:
        """Attach authoritative Pi runtime state to an existing thread."""
        current = self.registry.require(name)
        attached = Thread(
            name=current.name,
            tags=current.tags,
            worktree=current.worktree,
            parent=current.parent,
            task=current.task,
            pid=current.pid if pid is None else pid,
            session_file=str(Path(session_file).expanduser().resolve()),
        )
        self.register(attached)
        self.heartbeat(attached.name)
        return self.registry.require(attached.name)

    def stop(self, name: str) -> None:
        """Stop a registered participant and retain it in history."""
        with _store_lock(self._wire_lock_path):
            self._stop_unlocked(name)

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
            self.registry.unregister(canonical)

    def _stop_unlocked(self, name: str) -> None:
        thread = self.registry.require(name)
        if self.registry.status(name) in {
            ThreadStatus.STOPPED,
            ThreadStatus.ARCHIVED,
            ThreadStatus.DELETING,
        }:
            return
        if thread.pid <= 0 or thread.pid == os.getpid() or not self._process_alive(thread.pid):
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
            if sys.platform.startswith("linux"):
                stat = Path(f"/proc/{pid}/stat").read_text().split()
                if len(stat) > 2 and stat[2] == "Z":
                    return False
            else:
                status = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat="],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if not status or status.startswith("Z"):
                    return False
        except (OSError, ProcessLookupError):
            return False
        return True

    def _is_local_participant(self, thread: Thread) -> bool:
        """Prove a PID belongs to the named participant before signaling it."""
        if sys.platform == "darwin":
            import socket

            from .runtime import socket_path

            deadline = time.monotonic() + 2
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
            if self.registry.status(canonical) is not ThreadStatus.STOPPED:
                raise RelationViolationError("Stop a running thread before archiving it.")
            self.registry.archive(canonical)
            self.runtime_info.remove(canonical)

    def delete(self, name: str) -> DeleteThreadResult:
        """Remove a stopped thread and its owned state, preserving its children."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(name).name
            self.registry.begin_delete(canonical)
            messages_removed, markers_removed = self.bus.remove_thread(canonical)
            activity_removed = self.activity.remove_thread(canonical)
            runtime_removed = self.runtime_info.get(canonical) is not None
            self.runtime_info.remove(canonical)
            ledger_removed = self.ledger.remove_thread(canonical)
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

    def fork(self, spec: ForkSpec, pi_bin: str = "pi") -> Thread:
        with _store_lock(self._wire_lock_path):
            return self._fork_unlocked(spec, pi_bin)

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
        )
        self.registry.register(child)
        self.bus.mark_delivered_through(child.name, self.bus.latest_sequence())

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
                "AGENT_COMMS_ROOT": str(self.root.resolve()),
                "AGENT_COMMS_AGENT_BIN": pi_bin,
                "PI_PROMPT": spec.prompt or spec.task,
            }
        )

        args = [sys.executable, "-m", "agent_comms.worker"]
        # Non-interactive pi refuses to run without an explicit model, so pass
        # the parent's last model choice through to the child.
        model = _session_model(Path(parent.session_file))
        if model:
            provider, model_id = model
            env["AGENT_COMMS_AGENT_ARGS"] = f"--print --provider {provider} --model {model_id}"
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
