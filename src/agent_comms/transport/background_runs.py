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
import threading
import time
import uuid
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import ClassVar

from agent_comms.bus_context import BusContext
from agent_comms.execution.child_process import (
    OutlivesLauncherLifetime,
    Platform,
    ProcessIdentity,
    SpawnedProcess,
)
from agent_comms.installer.native_build_inputs import DetachedChildStdio
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, NullMember, PresentMember
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Terminal
from agent_comms.registry.registry_provenance import RegistryProvenance
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.locked_store import (
    DocumentChange,
    LockedStore,
    PatternFile,
    RuntimeFile,
)
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


class StoredBackgroundRun(DeclaredFamily, affix="Run"):
    """What one run record file holds: a detached run, or absence before write / after forget."""

    @abstractmethod
    def present(self) -> tuple[BackgroundRun, ...]: ...

    @abstractmethod
    def require_present(self) -> BackgroundRun: ...

    def settled(self, state: RunState) -> StoredBackgroundRun:
        return self

    def claimable(self, teller: ProcessIdentity) -> bool:
        return False

    def claimed(
        self, teller: ProcessIdentity,
    ) -> DocumentChange[StoredBackgroundRun, tuple[BackgroundRun, ...]]:
        return DocumentChange(self, ())

    def forgotten(self, now: float, snapshot: RegistryProvenance) -> bool:
        return False

    def files(self) -> tuple[Path, ...]:
        return ()


@dataclass(frozen=True)
class AbsentRun(NullMember, StoredBackgroundRun):
    """A run record file not yet published, or already retired."""


@dataclass(frozen=True, kw_only=True)
class BackgroundRun(PresentMember, StoredBackgroundRun, declared_name=None):
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

    def settled(self, state: RunState) -> StoredBackgroundRun:
        return replace(self, state=state) if self.state.may_become(state) else self

    def claimable(self, teller: ProcessIdentity) -> bool:
        return self.state.claimed(teller) is not self.state

    def claimed(
        self, teller: ProcessIdentity,
    ) -> DocumentChange[StoredBackgroundRun, tuple[BackgroundRun, ...]]:
        state = self.state.claimed(teller)
        if state is self.state:
            return DocumentChange(self, ())
        claimed = replace(self, state=state)
        return DocumentChange(claimed, (claimed,))

    def forgotten(self, now: float, snapshot: RegistryProvenance) -> bool:
        return self.state.forgotten(now, has_reader=self.thread.current(snapshot))


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
        with open(log, "ab") as output:
            child = SpawnedProcess.launch(
                ("/bin/bash", "-c", 'bash -c "$1"; echo $? > "$2"', "background-run",
                 command, str(cls.exit_file_of(log))),
                stdio=DetachedChildStdio(stdout=output),
                lifetime=OutlivesLauncherLifetime,
            )
        return cls(run_id=run_id, thread=thread, log=log, command=command, seconds=seconds,
                   process=child.identity, started_at=time.time())

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
class BackgroundRunStarted(ToolResult):
    """What the caller gets back at once: the run keeps going without the turn."""

    run_id: str
    log: Path


@dataclass(frozen=True, slots=True)
class BackgroundRunRecord(
    PatternFile, SingleFileStore, RuntimeFile, LockedStore[StoredBackgroundRun],
    DependentStateTier,
):
    """One detached run's durable record: its own file and lock, so no two runs contend."""

    filename: ClassVar[str] = "background_runs/{run_id}.json"
    record_type = StoredBackgroundRun

    @classmethod
    def for_run(cls, root: Path | str, run_id: str) -> BackgroundRunRecord:
        path = cls.path_in(root, run_id=run_id)
        if path.parent != cls.directory_in(root) / "background_runs" or path.stem != run_id:
            raise ValueError(f"Invalid background run id: {run_id!r}")
        return cls(path)

    @classmethod
    def all_in(cls, root: Path | str) -> tuple[BackgroundRunRecord, ...]:
        return tuple(cls(path) for path in sorted(cls.matching_in(cls.directory_in(root))))

    def empty(self) -> StoredBackgroundRun:
        return AbsentRun()

    def settle(self, state: RunState) -> None:
        current = self.read()
        if current.settled(state) is current:
            return
        self.update(lambda stored: DocumentChange.to(stored.settled(state)))

    def claim(self, teller: ProcessIdentity) -> tuple[BackgroundRun, ...]:
        if not self.read().claimable(teller):
            return ()
        return self.update(lambda stored: stored.claimed(teller))

    def forget(self, now: float, snapshot: RegistryProvenance) -> bool:
        if not self.read().forgotten(now, snapshot):
            return False
        files: tuple[Path, ...] = ()
        with self.guarded_write():
            latest = self._read_unlocked()
            if not latest.forgotten(now, snapshot):
                return False
            files = latest.files()
            self.retire()
        for path in files:
            path.unlink(missing_ok=True)
        return True


@dataclass(frozen=True, slots=True)
class BackgroundRunStore:
    """The detached runs on one bus, each in its own BackgroundRunRecord, and the watcher
    that settles them and tells their threads."""

    comms: BusContext
    watching: ClassVar[set[str]] = set()
    watching_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def of(cls, comms: BusContext) -> BackgroundRunStore:
        return cls(comms)

    @property
    def root(self) -> Path:
        return Path(self.comms.root)

    @property
    def log_directory(self) -> Path:
        return self.root / "logs"

    def record(self, run_id: str) -> BackgroundRunRecord:
        return BackgroundRunRecord.for_run(self.root, run_id)

    def records(self) -> tuple[BackgroundRunRecord, ...]:
        return BackgroundRunRecord.all_in(self.root)

    def runs(self) -> tuple[BackgroundRun, ...]:
        found = [
            run for record in self.records()
            for run in record.read().present()
        ]
        found.sort(key=lambda run: (run.started_at, run.run_id))
        return tuple(found)

    def for_thread(self, thread: ThreadIncarnation) -> tuple[BackgroundRun, ...]:
        return tuple(run for run in self.runs() if run.thread == thread)

    def start(self, run: BackgroundRun) -> BackgroundRunStarted:
        """Record a run in its own file and watch it to its end."""
        self.record(run.run_id).replace(run)
        self.watch(run)
        return BackgroundRunStarted(run_id=run.run_id, log=run.log)

    def recover(self) -> None:
        """What every tool touch does first. A run no watcher here holds takes the state a
        touch finds it in now (ended if its process is gone, owed again if its teller died)
        and is watched if it is still running. Settled runs are never rewritten."""
        with self.watching_lock:
            watched_here = frozenset(self.watching)
        for record in self.records():
            for run in record.read().present():
                if run.run_id in watched_here:
                    continue
                found = run.state.recovered(run)
                if found is not run.state:
                    record.settle(found)
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
        self.record(run_id).settle(state)

    def tell(self) -> None:
        """Tell each thread about its ended runs. Told runs past retention, and ended runs
        whose thread no longer exists, are forgotten first, so a later thread of the same
        name is never told. A run is told only once its notice is on the bus: a delivery
        that raises leaves it, and every run claimed after it, untold, and the error reaches
        whoever touched the store."""
        now = time.time()
        snapshot = self.comms.registry.snapshot()
        teller = ProcessIdentity.current()
        telling: list[BackgroundRun] = []
        for record in self.records():
            if record.forget(now, snapshot):
                continue
            telling.extend(record.claim(teller))
        telling.sort(key=lambda run: (run.started_at, run.run_id))
        try:
            while telling:
                run = telling[0]
                BackgroundRunNotice().publish(
                    self.comms.messaging, ThreadTarget(run.thread.resolved(snapshot).name),
                    run.notice())
                self.settle(run.run_id, run.state.delivered(time.time()))
                del telling[0]
        finally:
            for run in telling:
                self.settle(run.run_id, run.state.undelivered())
