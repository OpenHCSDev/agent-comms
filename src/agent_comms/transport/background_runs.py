"""Background runs: work a thread starts detached and is told about when it ends.

A run is a record in a durable store. A shell command and a deferred tool call are the two
members of one family: each names the process whose life is the run's, where its output goes
and where it stands. One watcher waits for a run's end, records the state it reached and
tells the thread; every tool touch first recovers the runs a bridge restart left behind, so a
run that outlives its bridge is watched again and one that ended in the meantime is reported.
"""

from __future__ import annotations

import math
import os
import subprocess
import threading
import time
import uuid
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from agent_comms.execution.child_process import (
    Platform,
    ProcessIdentity,
    ResidentManager,
)
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.messaging.sending import Messaging
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Terminal
from agent_comms.storage.locked_store import DocumentChange, LockedStore, RuntimeFile
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore
from agent_comms.task_attachments.owner_notices import BackgroundRunNotice
from agent_comms.transport.tool_result import ToolResult

DEFAULT_SECONDS = 3600
TAIL_BYTES = 2000
KILL_GRACE = 30.0
RETENTION = 24 * 3600


@dataclass(frozen=True)
class RunState(DeclaredFamily, LifecycleState, affix="State"):
    """Where a run stands; an ended state carries what its end left behind."""

    @property
    @abstractmethod
    def outcome(self) -> str:
        """The run's standing as its thread reads it, after the words 'Background run ID'."""

    def expired(self, now: float) -> bool:
        """Whether a reported run in this state is old enough to forget."""
        return False


@dataclass(frozen=True)
class RunningState(RunState):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return (EndedState,)

    @property
    def outcome(self) -> str:
        return "is running"


@dataclass(frozen=True)
class EndedState(Terminal, RunState, declared_name=None):
    at: float

    def expired(self, now: float) -> bool:
        return now - self.at > RETENTION


@dataclass(frozen=True)
class ExitedState(EndedState):
    code: int

    @property
    def outcome(self) -> str:
        return f"exited with code {self.code}"


@dataclass(frozen=True)
class KilledState(EndedState):
    @property
    def outcome(self) -> str:
        return "hit its deadline and was killed"


@dataclass(frozen=True)
class ReturnedState(EndedState):
    @property
    def outcome(self) -> str:
        return "returned its result"


@dataclass(frozen=True)
class RaisedState(EndedState):
    @property
    def outcome(self) -> str:
        return "failed"


@dataclass(frozen=True)
class GoneState(EndedState):
    @property
    def outcome(self) -> str:
        return "is gone: its process ended without leaving an outcome"


@dataclass(frozen=True, kw_only=True)
class BackgroundRun(DeclaredFamily, affix="Run"):
    """One detached run: who asked, the process whose life is the run's, where its output
    goes, where it stands and whether its thread has been told how it ended."""

    run_id: str
    thread: str
    log: str
    process: ProcessIdentity
    started_at: float
    state: RunState = RunningState()
    reported: bool = False

    @staticmethod
    def named(directory: Path, *parts: str) -> tuple[str, str]:
        """A fresh run id and the log file it owns."""
        directory.mkdir(parents=True, exist_ok=True)
        run_id = "-".join((*parts, uuid.uuid4().hex[:8]))
        return run_id, str(directory / f"{run_id}.log")

    @property
    @abstractmethod
    def work(self) -> str:
        """What runs, as one line of the notice."""

    @abstractmethod
    def wait(self) -> RunState:
        """Block until the process ends and answer the state its end left. The same wait
        serves a run this bridge started and one a restart left behind."""

    def tail(self, limit: int = TAIL_BYTES) -> str:
        try:
            return Path(self.log).read_bytes()[-limit:].decode(errors="replace")
        except FileNotFoundError:
            return "(no output)"

    def notice(self) -> str:
        return (f"Background run {self.run_id} {self.state.outcome}.\n{self.work}\n"
                f"last {TAIL_BYTES} bytes of output:\n{self.tail()}")

    def files(self) -> tuple[Path, ...]:
        return (Path(self.log),)


@dataclass(frozen=True, kw_only=True)
class ShellRun(BackgroundRun):
    """A detached shell command; its process is the session leader that runs it."""

    command: str
    seconds: int

    @classmethod
    def launched(cls, directory: Path, thread: str, command: str, seconds: int) -> ShellRun:
        """The one detached-shell launch: a session leader writing to the owned log, in its
        own scope under the host's resident manager, because a bridge that is a systemd unit
        takes its whole cgroup with it when it restarts. The leader records the command's
        exit code in a file: after a restart the command is nobody's child and its code can
        be read nowhere else."""
        run_id, log = cls.named(directory, thread)
        manager = ResidentManager.current()
        with open(log, "ab") as output:
            process = subprocess.Popen(
                manager.command((
                    "/bin/bash", "-c", 'bash -c "$1"; echo $? > "$2"', "background-run",
                    command, str(cls.exit_file_of(log)))),
                stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True, env=manager.handoff_environment(os.environ),
            )
        return cls(run_id=run_id, thread=thread, log=log, command=command, seconds=seconds,
                   process=Platform.current().identity(process.pid), started_at=time.time())

    @staticmethod
    def exit_file_of(log: str) -> Path:
        return Path(log).with_suffix(".exit")

    @property
    def exit_file(self) -> Path:
        return self.exit_file_of(self.log)

    @property
    def work(self) -> str:
        return f"command: {self.command}"

    def files(self) -> tuple[Path, ...]:
        return (*super().files(), self.exit_file)

    def wait(self) -> RunState:
        platform = Platform.current()
        deadline = self.started_at + self.seconds
        platform.wait_any_exited((self.process,), deadline - time.time())
        if time.time() < deadline or not platform.matches(self.process):
            return self.left_behind()
        platform.terminate_group(self.process)
        platform.wait_any_exited((self.process,), KILL_GRACE)
        if platform.matches(self.process):
            platform.force_group(self.process)
        return KilledState(at=time.time())

    def left_behind(self) -> RunState:
        try:
            return ExitedState(at=time.time(), code=int(self.exit_file.read_text()))
        except FileNotFoundError:
            return GoneState(at=time.time())


@dataclass(frozen=True, kw_only=True)
class ToolCallRun(BackgroundRun):
    """A tool call applied on a bridge thread; its process is that bridge. The call cannot
    outlive its bridge, so a restart leaves it gone, never running."""

    tool: str

    @classmethod
    def hosted(cls, directory: Path, thread: str, tool: str) -> ToolCallRun:
        run_id, log = cls.named(directory, thread, tool)
        return cls(run_id=run_id, thread=thread, log=log, tool=tool,
                   process=ProcessIdentity.current(), started_at=time.time())

    @property
    def work(self) -> str:
        return f"tool: {self.tool}"

    def wait(self) -> RunState:
        Platform.current().wait_any_exited((self.process,), math.inf)
        return GoneState(at=time.time())


@dataclass(frozen=True)
class BackgroundRuns:
    """Every background run this bus knows, keyed by its run id."""

    runs: dict[str, BackgroundRun] = field(default_factory=dict)


@dataclass(frozen=True)
class BackgroundRunStarted(ToolResult):
    """What the caller gets back at once: the run keeps going without the turn."""

    run_id: str
    log: str


@dataclass(frozen=True, slots=True)
class BackgroundRunStore(
    SingleFileStore, RuntimeFile, LockedStore[BackgroundRuns], DependentStateTier,
):
    """The durable record of detached runs, and the one watcher that settles and reports them."""

    filename: ClassVar[str] = "background_run_records.json"
    record_type = BackgroundRuns
    watching: ClassVar[set[str]] = set()
    watching_lock: ClassVar[threading.Lock] = threading.Lock()

    def empty(self) -> BackgroundRuns:
        return BackgroundRuns()

    @property
    def log_directory(self) -> Path:
        return self.path.parent / "logs"

    def for_thread(self, thread: str) -> tuple[BackgroundRun, ...]:
        return tuple(run for run in self.read().runs.values() if run.thread == thread)

    def start(self, run: BackgroundRun, wait: Callable[[], RunState],
              messaging: Messaging) -> BackgroundRunStarted:
        """Record a run and watch it to its end; `wait` blocks until then and answers the
        state reached."""
        self.recover(messaging)
        self.update(lambda current: DocumentChange.to(replace(
            current, runs=current.runs | {run.run_id: run})))
        self.watch(run, wait, messaging)
        return BackgroundRunStarted(run_id=run.run_id, log=run.log)

    def recover(self, messaging: Messaging) -> None:
        """What every tool touch does first: watch the running runs no watcher here holds
        (a bridge restart left them), report the ended ones nobody was told about, and
        forget reported runs past their retention."""
        for run in self.read().runs.values():
            if run.state.successors():
                self.watch(run, run.wait, messaging)
        self.report(messaging)
        self.forget(time.time())

    def watch(self, run: BackgroundRun, wait: Callable[[], RunState],
              messaging: Messaging) -> None:
        """A thread per run: the bridge outlives turns, and wait-settle-report is all the
        watcher does. A run already watched in this process keeps its one watcher."""
        with self.watching_lock:
            if run.run_id in self.watching:
                return
            self.watching.add(run.run_id)

        def watched() -> None:
            try:
                self.settle(run.run_id, wait())
                self.report(messaging)
            finally:
                with self.watching_lock:
                    self.watching.discard(run.run_id)

        threading.Thread(target=watched, daemon=True, name=f"background-{run.run_id}").start()

    def settle(self, run_id: str, state: RunState) -> None:
        """A running run takes the state its end left; an ended run keeps the one it has,
        so two watchers of one run cannot disagree about how it ended."""
        self.update(lambda current: DocumentChange.to(replace(current, runs={
            key: replace(run, state=state)
            if key == run_id and run.state.may_become(state) else run
            for key, run in current.runs.items()
        })))

    def report(self, messaging: Messaging) -> None:
        """Tell each thread about its ended runs nobody reported. The claim is one store
        transaction, so a run is reported once however many watchers and touches see it."""

        def claimed(current: BackgroundRuns) -> DocumentChange[
                BackgroundRuns, tuple[BackgroundRun, ...]]:
            owed = tuple(run for run in current.runs.values()
                         if not run.reported and not run.state.successors())
            return DocumentChange(replace(current, runs=current.runs | {
                run.run_id: replace(run, reported=True) for run in owed}), owed)

        for run in self.update(claimed):
            try:
                BackgroundRunNotice().publish(messaging, ThreadTarget(run.thread), run.notice())
            except Exception as error:  # noqa: BLE001 - a dead delivery never kills the watcher
                with open(run.log, "a", encoding="utf-8") as log:
                    log.write(
                        f"\n[background-run] notify failed: {type(error).__name__}: {error}\n")

    def forget(self, now: float) -> None:
        def kept(current: BackgroundRuns) -> DocumentChange[
                BackgroundRuns, tuple[BackgroundRun, ...]]:
            dropped = tuple(run for run in current.runs.values()
                            if run.reported and run.state.expired(now))
            return DocumentChange(replace(current, runs={
                key: run for key, run in current.runs.items() if run not in dropped}), dropped)

        for run in self.update(kept):
            for path in run.files():
                path.unlink(missing_ok=True)
