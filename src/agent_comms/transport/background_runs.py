"""Background runs: work a thread starts detached and is told about when it ends.

A run is a record in a durable store. A shell command and a deferred tool call are the two
members of one family: each names the process whose life is the run's, where its output goes
and where it stands. A run is running, then ended, then being told to its thread, then told;
a telling that fails leaves the run untold and raises, so the next touch tries again. One
watcher waits for a run's end; every tool touch first recovers the runs a bridge restart
left behind, so a run that outlives its bridge is watched again and one that ended in the
meantime is told.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
import uuid
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import ClassVar

from agent_comms.comms import Comms
from agent_comms.execution.child_process import (
    Platform,
    ProcessIdentity,
    ResidentManager,
)
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Terminal
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.locked_store import DocumentChange, LockedStore, RuntimeFile
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore
from agent_comms.task_attachments.owner_notices import BackgroundRunNotice
from agent_comms.transport.tool_result import ToolResult

DEFAULT_SECONDS = 3600
TAIL_BYTES = 2000
KILL_GRACE = 30.0
RETENTION = 24 * 3600


@dataclass(frozen=True)
class RunOutcome(DeclaredFamily, affix="Outcome"):
    """How a run ended, and when."""

    at: float

    @property
    @abstractmethod
    def summary(self) -> str:
        """The outcome as its thread reads it, after the words 'Background run ID'."""


@dataclass(frozen=True)
class ExitedOutcome(RunOutcome):
    code: int

    @property
    def summary(self) -> str:
        return f"exited with code {self.code}"


@dataclass(frozen=True)
class KilledOutcome(RunOutcome):
    @property
    def summary(self) -> str:
        return "hit its deadline and was killed"


@dataclass(frozen=True)
class ReturnedOutcome(RunOutcome):
    @property
    def summary(self) -> str:
        return "returned its result"


@dataclass(frozen=True)
class RaisedOutcome(RunOutcome):
    @property
    def summary(self) -> str:
        return "failed"


@dataclass(frozen=True)
class GoneOutcome(RunOutcome):
    @property
    def summary(self) -> str:
        return "is gone: its process ended without leaving an outcome"


@dataclass(frozen=True)
class RunState(DeclaredFamily, LifecycleState, affix="State"):
    """Where a run stands. Each member answers what a store touch does with a run in it,
    so the store asks the state and tests none."""

    @property
    @abstractmethod
    def summary(self) -> str: ...

    def recovered(self, run: BackgroundRun) -> RunState:
        """This state as a touch finds it now, for a run no watcher here holds."""
        return self

    def watched(self, store: BackgroundRunStore, run: BackgroundRun) -> None:
        """Only a running run has an end to wait for."""

    def claimed(self, teller: ProcessIdentity) -> RunState:
        """Only an ended run is owed a telling."""
        return self

    @abstractmethod
    def forgotten(self, now: float, *, has_reader: bool) -> bool:
        """Whether the store drops a run in this state."""


@dataclass(frozen=True)
class RunningState(RunState):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return (EndedState,)

    @property
    def summary(self) -> str:
        return "is running"

    def recovered(self, run: BackgroundRun) -> RunState:
        return self if run.alive() else EndedState(run.left_behind())

    def watched(self, store: BackgroundRunStore, run: BackgroundRun) -> None:
        store.watch(run)

    def forgotten(self, now: float, *, has_reader: bool) -> bool:
        """A running run is kept, so its deadline still ends it."""
        return False


@dataclass(frozen=True)
class OutcomeState(RunState, declared_name=None):
    """A run that has ended carries how."""

    outcome: RunOutcome

    @property
    def summary(self) -> str:
        return self.outcome.summary

    def forgotten(self, now: float, *, has_reader: bool) -> bool:
        """An ended run waits for its thread, unless that thread no longer exists."""
        return not has_reader


@dataclass(frozen=True)
class EndedState(OutcomeState):
    """Ended, and its thread has not been told."""

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return (TellingState,)

    def claimed(self, teller: ProcessIdentity) -> RunState:
        return TellingState(self.outcome, teller)


@dataclass(frozen=True)
class UntoldState(EndedState):
    """A telling failed, or its teller died holding it: the thread is owed again. Its own
    member, so a second watcher's 'ended' can never reopen a telling in progress."""


@dataclass(frozen=True)
class TellingState(OutcomeState):
    """One process holds the telling. It ends told, or untold."""

    teller: ProcessIdentity

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return (ToldState, UntoldState)

    def recovered(self, run: BackgroundRun) -> RunState:
        return self if Platform.current().matches(self.teller) else self.undelivered()

    def undelivered(self) -> UntoldState:
        return UntoldState(self.outcome)

    def delivered(self, at: float) -> ToldState:
        return ToldState(self.outcome, at)


@dataclass(frozen=True)
class ToldState(Terminal, OutcomeState):
    """Its thread was told: the notice is on the bus."""

    told_at: float

    def forgotten(self, now: float, *, has_reader: bool) -> bool:
        return not has_reader or now - self.told_at > RETENTION


@dataclass(frozen=True, kw_only=True)
class BackgroundRun(DeclaredFamily, affix="Run"):
    """One detached run: which thread asked, the process whose life is the run's, where its
    output goes and where it stands."""

    run_id: str
    thread: ThreadIncarnation
    log: Path
    process: ProcessIdentity
    started_at: float
    state: RunState = RunningState()

    @staticmethod
    def named(directory: Path, *parts: str) -> tuple[str, Path]:
        """A fresh run id and the log file it owns."""
        directory.mkdir(parents=True, exist_ok=True)
        run_id = "-".join((*parts, uuid.uuid4().hex[:8]))
        return run_id, directory / f"{run_id}.log"

    @property
    @abstractmethod
    def work(self) -> str:
        """What runs, as one line of the notice."""

    @abstractmethod
    def wait(self) -> RunOutcome:
        """Block until the run ends and answer how. The same wait serves a run this bridge
        started and one a restart left behind."""

    @abstractmethod
    def left_behind(self) -> RunOutcome:
        """The outcome a process that has already ended left for this run."""

    def alive(self) -> bool:
        return Platform.current().matches(self.process)

    def tail(self, limit: int = TAIL_BYTES) -> str:
        try:
            return self.log.read_bytes()[-limit:].decode(errors="replace")
        except FileNotFoundError:
            return "(no output)"

    def notice(self) -> str:
        return (f"Background run {self.run_id} {self.state.summary}.\n{self.work}\n"
                f"last {TAIL_BYTES} bytes of output:\n{self.tail()}")

    def files(self) -> tuple[Path, ...]:
        return (self.log,)


@dataclass(frozen=True, kw_only=True)
class ShellRun(BackgroundRun):
    """A detached shell command; its process is the session leader that runs it."""

    command: str
    seconds: int

    @classmethod
    def launched(
        cls, directory: Path, thread: ThreadIncarnation, command: str, seconds: int,
    ) -> ShellRun:
        """The one detached-shell launch: a session leader writing to the owned log, in its
        own scope under the host's resident manager, because a bridge that is a systemd unit
        takes its whole cgroup with it when it restarts. The leader records the command's
        exit code in a file: after a restart the command is nobody's child and its code can
        be read nowhere else."""
        run_id, log = cls.named(directory, thread.name)
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
    def exit_file_of(log: Path) -> Path:
        return log.with_suffix(".exit")

    @property
    def exit_file(self) -> Path:
        return self.exit_file_of(self.log)

    @property
    def work(self) -> str:
        return f"command: {self.command}"

    def files(self) -> tuple[Path, ...]:
        return (*super().files(), self.exit_file)

    def wait(self) -> RunOutcome:
        platform = Platform.current()
        deadline = self.started_at + self.seconds
        platform.wait_any_exited((self.process,), deadline - time.time())
        if time.time() < deadline or not self.alive():
            return self.left_behind()
        platform.terminate_group(self.process)
        platform.wait_any_exited((self.process,), KILL_GRACE)
        if self.alive():
            platform.force_group(self.process)
        return KilledOutcome(time.time())

    def left_behind(self) -> RunOutcome:
        try:
            return ExitedOutcome(time.time(), int(self.exit_file.read_text()))
        except FileNotFoundError:
            return GoneOutcome(time.time())


@dataclass(frozen=True, kw_only=True)
class ToolCallRun(BackgroundRun):
    """A tool call applied on a bridge thread; its process is that bridge. The call lives
    in that process and nowhere else, so a restart leaves it gone, never running."""

    tool: str
    hosted_here: ClassVar[dict[str, Callable[[], RunOutcome]]] = {}

    @classmethod
    def hosted(
        cls, directory: Path, thread: ThreadIncarnation, tool: str,
        call: Callable[[], ToolResult],
    ) -> ToolCallRun:
        """A call this process will apply when the run is waited on."""
        run_id, log = cls.named(directory, thread.name, tool)
        run = cls(run_id=run_id, thread=thread, log=log, tool=tool,
                  process=ProcessIdentity.current(), started_at=time.time())
        cls.hosted_here[run_id] = partial(run.applied, call)
        return run

    @property
    def work(self) -> str:
        return f"tool: {self.tool}"

    def wait(self) -> RunOutcome:
        """Apply the call this process hosts. A run another bridge hosts has no call here:
        its end is that bridge's exit."""
        return self.hosted_here.pop(self.run_id, self.outlived)()

    def applied(self, call: Callable[[], ToolResult]) -> RunOutcome:
        """The call's result, or its failure, is the run's output."""
        try:
            self.log.write_text(json.dumps(
                FieldCodec.encode(call().to_wire()), default=str), encoding="utf-8")
        except Exception as error:  # noqa: BLE001 - the failure is the run's reported outcome
            self.log.write_text(f"{type(error).__name__}: {error}", encoding="utf-8")
            return RaisedOutcome(time.time())
        return ReturnedOutcome(time.time())

    def outlived(self) -> RunOutcome:
        Platform.current().wait_any_exited((self.process,), math.inf)
        return self.left_behind()

    def left_behind(self) -> RunOutcome:
        return GoneOutcome(time.time())


@dataclass(frozen=True)
class BackgroundRuns:
    """Every background run this bus knows, keyed by its run id."""

    runs: dict[str, BackgroundRun] = field(default_factory=dict)


@dataclass(frozen=True)
class BackgroundRunStarted(ToolResult):
    """What the caller gets back at once: the run keeps going without the turn."""

    run_id: str
    log: Path


@dataclass(frozen=True, slots=True)
class BackgroundRunStore(
    SingleFileStore, RuntimeFile, LockedStore[BackgroundRuns], DependentStateTier,
):
    """The durable record of detached runs on one bus, and the one watcher that settles
    them and tells their threads."""

    comms: Comms
    filename: ClassVar[str] = "background_run_records.json"
    record_type = BackgroundRuns
    watching: ClassVar[set[str]] = set()
    watching_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def of(cls, comms: Comms) -> BackgroundRunStore:
        return cls.at(comms.root, comms)

    def empty(self) -> BackgroundRuns:
        return BackgroundRuns()

    @property
    def log_directory(self) -> Path:
        return self.path.parent / "logs"

    def for_thread(self, thread: ThreadIncarnation) -> tuple[BackgroundRun, ...]:
        return tuple(run for run in self.read().runs.values() if run.thread == thread)

    def start(self, run: BackgroundRun) -> BackgroundRunStarted:
        """Record a run and watch it to its end."""
        self.update(lambda current: DocumentChange.to(replace(
            current, runs=current.runs | {run.run_id: run})))
        self.watch(run)
        return BackgroundRunStarted(run_id=run.run_id, log=run.log)

    def recover(self) -> None:
        """What every tool touch does first. A run a bridge restart left takes the state a
        touch finds it in now (ended if its process is gone, owed again if its teller died)
        and is watched if it is still running. Then every ended run is told."""
        for run in self.read().runs.values():
            found = run.state.recovered(run)
            self.settle(run.run_id, found)
            found.watched(self, run)
        self.tell()

    def watch(self, run: BackgroundRun) -> None:
        """A thread per run: the bridge outlives turns, and wait-settle-tell is all the
        watcher does. A run already watched in this process keeps its one watcher."""
        with self.watching_lock:
            if run.run_id in self.watching:
                return
            self.watching.add(run.run_id)

        def watched() -> None:
            try:
                self.settle(run.run_id, EndedState(run.wait()))
                self.tell()
            finally:
                with self.watching_lock:
                    self.watching.discard(run.run_id)

        threading.Thread(target=watched, daemon=True, name=f"background-{run.run_id}").start()

    def settle(self, run_id: str, state: RunState) -> None:
        """A run takes a state only its own state allows next, so two watchers of one run
        cannot disagree about how it ended and a told run stays told."""
        self.update(lambda current: DocumentChange.to(replace(current, runs={
            key: replace(run, state=state)
            if key == run_id and run.state.may_become(state) else run
            for key, run in current.runs.items()
        })))

    def tell(self) -> None:
        """Tell each thread about its ended runs. Told runs past retention, and ended runs
        whose thread no longer exists, are forgotten first, so a later thread of the same
        name is never told. Claiming the rest is one store transaction, so a run is told
        once however many watchers and touches see it. A run is told only once its notice
        is on the bus: a delivery that raises leaves it, and every run claimed after it,
        untold, and the error reaches whoever touched the store."""
        self.forget(time.time())
        teller = ProcessIdentity.current()

        def claimed(current: BackgroundRuns) -> DocumentChange[
                BackgroundRuns, tuple[BackgroundRun, ...]]:
            mine = {
                key: replace(run, state=state) for key, run in current.runs.items()
                if (state := run.state.claimed(teller)) is not run.state
            }
            return DocumentChange(
                replace(current, runs=current.runs | mine), tuple(mine.values()))

        telling = list(self.update(claimed))
        try:
            while telling:
                run = telling[0]
                BackgroundRunNotice().publish(
                    self.comms.messaging, ThreadTarget(run.thread.name), run.notice())
                self.settle(run.run_id, run.state.delivered(time.time()))
                del telling[0]
        finally:
            for run in telling:
                self.settle(run.run_id, run.state.undelivered())

    def has_reader(self, run: BackgroundRun) -> bool:
        """Whether the thread that started the run still exists: a later thread of the same
        name is a different reader."""
        registry = self.comms.registry
        return (run.thread.name in registry
                and registry.require(run.thread.name).incarnation == run.thread)

    def forget(self, now: float) -> None:
        def kept(current: BackgroundRuns) -> DocumentChange[
                BackgroundRuns, tuple[BackgroundRun, ...]]:
            dropped = tuple(
                run for run in current.runs.values()
                if run.state.forgotten(now, has_reader=self.has_reader(run)))
            return DocumentChange(replace(current, runs={
                key: run for key, run in current.runs.items() if run not in dropped}), dropped)

        for run in self.update(kept):
            for path in run.files():
                path.unlink(missing_ok=True)
