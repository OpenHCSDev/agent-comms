"""Background tool runs: start a shell command detached, get told when it's done.

A run is a record in a durable store, so a bridge restart cannot lose it: the next tool
touch re-adopts live runs and reports the ones that finished while the bridge was down.
Completion is delivered as a message to the requesting thread, which the delivery machinery
steers into a live turn or queues for the next one — the same road every message takes.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.storage.locked_store import (
    DocumentChange,
    LockedStore,
    RuntimeFile,
)
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore
from agent_comms.execution.child_process import Platform
from agent_comms.messaging.messages import NoticeRole
from agent_comms.transport.request import tool_field
from agent_comms.transport.tool_request import ThreadToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError

DEFAULT_SECONDS = 3600
TAIL_BYTES = 2000


@dataclass(frozen=True)
class BackgroundRunRecord:
    """One detached run: who asked, what runs, where the output goes, what it costs."""

    run_id: str
    thread: str
    command: str
    log: str
    pid: int
    seconds: int
    started_at: float = 0.0


@dataclass(frozen=True)
class BackgroundRuns:
    """Every background run this bridge knows, keyed by its run id."""

    runs: dict[str, BackgroundRunRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class BackgroundRunHandle(ToolResult):
    """What the caller gets back immediately: the run keeps going without the turn."""

    run_id: str
    pid: int
    log: str


@dataclass(frozen=True)
class BackgroundStatusRead(ToolResult):
    """One run's state, or every run this thread owns."""

    runs: tuple[dict[str, str | int], ...]


def _tail(log: Path, limit: int = TAIL_BYTES) -> str:
    try:
        return log.read_bytes()[-limit:].decode(errors="replace")
    except FileNotFoundError:
        return "(no output)"


@dataclass(frozen=True, slots=True)
class BackgroundRunStore(
    SingleFileStore, RuntimeFile, LockedStore[BackgroundRuns], DependentStateTier,
):
    """The durable record of detached runs; the notification is the delivery path's job."""

    filename: ClassVar[str] = "background_runs.json"
    record_type = BackgroundRuns

    def empty(self) -> BackgroundRuns:
        return BackgroundRuns()

    def add(self, run: BackgroundRunRecord) -> None:
        self.update(lambda current: DocumentChange.to(replace(
            current, runs=current.runs | {run.run_id: run})))

    def for_thread(self, thread: str) -> tuple[BackgroundRunRecord, ...]:
        return tuple(run for run in self.read().runs.values() if run.thread == thread)

    def record_started(self, run_id: str, started_at: float) -> None:
        self.update(lambda current: DocumentChange.to(replace(
            current, runs={
                key: replace(value, started_at=started_at) if key == run_id else value
                for key, value in current.runs.items()
            })))


def _launch_detached(command: str, log: Path) -> subprocess.Popen:
    """The one detached-shell launch: session leader, output to the owned log."""
    return subprocess.Popen(
        ["/bin/bash", "-c", command],
        stdout=open(log, "ab"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def _notify(run: BackgroundRunRecord, send: Callable[..., object], outcome: str) -> None:
    tail = _tail(Path(run.log))
    body = (f"Background run {run.run_id} {outcome}.\n"
            f"command: {run.command}\nlast {TAIL_BYTES} bytes of output:\n{tail}")
    try:
        send(run.thread, body, role=NoticeRole())
    except Exception as error:  # noqa: BLE001 - a dead delivery never kills the watcher
        with open(Path(run.log), "a", encoding="utf-8") as log:
            log.write(f"\n[background-run] notify failed: {type(error).__name__}: {error}\n")


def _watch(run: BackgroundRunRecord, send: Callable[..., object], store: BackgroundRunStore) -> None:
    """Watch one run to its exit, then notify the requesting thread. A thread per run:
    the bridge outlives turns, and wait-then-notify is all the watcher does."""
    identity = Platform.current().identity(run.pid)

    def watch() -> None:
        started = time.time()
        store.record_started(run.run_id, started)
        try:
            Platform.current().wait_any_exited((identity,), run.seconds)
        except TimeoutError:
            Platform.current().terminate_group(identity)
            Platform.current().wait_any_exited((identity,), 30.0)
            _notify(run, send, "hit its deadline and was killed")
            return
        _notify(run, send, "finished")

    threading.Thread(target=watch, daemon=True, name=f"background-{run.run_id}").start()


@dataclass(frozen=True, kw_only=True)
class CommsRunBackgroundTool(ThreadToolRequest, declared_name="run_background"):
    label = "Run in background"
    description = (
        "Start a shell command detached and return immediately: the turn keeps going while "
        "the command runs. Its output streams to a log file this tool owns; when the command "
        "exits (or its deadline passes), a message with the outcome and the log tail is sent "
        "to this thread. Use background_status to poll or tail earlier."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    command: Annotated[str, *tool_field("Shell command to run detached")]
    seconds: Annotated[int, *tool_field("Kill the command after this many seconds")] = (
        DEFAULT_SECONDS
    )

    def apply(self, comms, caller) -> BackgroundRunHandle:
        if not self.command.strip():
            raise ToolRefusedError("A background command cannot be empty.")
        store = BackgroundRunStore.at(comms.root)
        log_dir = store.path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"{self.thread}-{uuid.uuid4().hex[:8]}"
        log = log_dir / f"{run_id}.log"
        process = _launch_detached(self.command, log)
        run = BackgroundRunRecord(
            run_id=run_id, thread=self.thread, command=self.command, log=str(log),
            pid=process.pid, seconds=self.seconds)
        store.add(run)

        def send(target: str, body: str, **kwargs: object) -> object:
            return comms.messaging.send(self.thread, target, body, **kwargs)

        _watch(run, send, store)
        return BackgroundRunHandle(run_id=run_id, pid=process.pid, log=str(log))


@dataclass(frozen=True, kw_only=True)
class CommsBackgroundStatusTool(ThreadToolRequest, declared_name="background_status"):
    label = "Background status"
    description = (
        "Report one background run's state (running, finished, or gone) and the tail of its "
        "log; without run_id, list every background run this thread owns. This also re-adopts "
        "runs a bridge restart left behind."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    run_id: Annotated[str, *tool_field("Run id from run_background; empty lists all")] = ""
    tail_bytes: Annotated[int, *tool_field("Bytes of log tail to return")] = TAIL_BYTES

    def apply(self, comms, caller) -> BackgroundStatusRead:
        store = BackgroundRunStore.at(comms.root)
        platform = Platform.current()
        rows = []
        for run in store.for_thread(self.thread):
            if self.run_id and run.run_id != self.run_id:
                continue
            try:
                alive = platform.matches(platform.identity(run.pid))
            except ProcessLookupError:
                alive = False
            rows.append({
                "run_id": run.run_id, "command": run.command, "pid": run.pid,
                "state": "running" if alive else "finished", "log": run.log,
                "tail": _tail(Path(run.log), self.tail_bytes),
            })
        return BackgroundStatusRead(runs=tuple(rows))
