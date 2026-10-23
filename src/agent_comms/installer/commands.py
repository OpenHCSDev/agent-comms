"""Process execution, diagnostics and timing for one installation."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from agent_comms.child_process import SpawnedProcess
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.interpreter_operation import (
    ProbeProgram,
    ProbeRequest,
    ProbeReply,
    RuntimeProbe,
    SourceProgram,
)
from agent_comms.installer.interpreter_operation import CommandPhase, GitPhase, InstallClock, InstallPhase, PhaseTiming, WorkerClosePhase
from agent_comms.installer.refusals import InterpreterWorkerDied, UpgradeRefused
from agent_comms.launch_runtime import RestoredRuntime
from agent_comms.maintenance_barrier import MaintenanceHolder
from agent_comms.repo import Repo


@dataclass(frozen=True)
class InterpreterWorkerKey:
    python: Path
    program: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]




@dataclass
class InterpreterWorker:
    """One interpreter answering requests on its own pipes; stderr is kept for refusals."""

    child: SpawnedProcess
    requests: BinaryIO
    answers: BinaryIO
    diagnostics: BinaryIO
    exchange_lock: Lock = field(default_factory=Lock)

    @classmethod
    def start(cls, key: InterpreterWorkerKey) -> InterpreterWorker:
        with ExitStack() as owned:
            diagnostics = owned.enter_context(tempfile.TemporaryFile())
            request_read, request_write = os.pipe()
            answer_read, answer_write = os.pipe()
            requests = owned.enter_context(os.fdopen(request_write, "wb"))
            answers = owned.enter_context(os.fdopen(answer_read, "rb"))
            try:
                child = SpawnedProcess.launch(
                    (str(key.python), "-P", *key.program), cwd=key.cwd,
                    env=dict(key.environment), runtime=RestoredRuntime(key.python),
                    stdin=request_read, output=answer_write, stderr=diagnostics,
                )
            finally:
                os.close(request_read)
                os.close(answer_write)
            worker = cls(child, requests, answers, diagnostics)
            owned.pop_all()
        return worker

    def errors(self) -> str:
        self.diagnostics.seek(0)
        return self.diagnostics.read().decode(errors="replace").strip()

    def exchange(self, encoded: str) -> str:
        with self.exchange_lock:
            self.requests.write((encoded + "\n").encode())
            self.requests.flush()
            answer = self.answers.readline()
            if not answer:
                raise InterpreterWorkerDied(self.child.reap(), self.errors())
            return answer.decode()

    def finish(self) -> None:
        """Closing its requests ends the worker's loop, so its interpreter starts exiting."""
        self.requests.close()

    def close(self) -> None:
        """A finished worker is reaped and its streams closed."""
        with ExitStack() as closing:
            closing.callback(self.diagnostics.close)
            closing.callback(self.answers.close)
            closing.callback(self.child.close)
            self.finish()
            self.child.reap()


@dataclass
class InstallCommands:
    """The commands one install runs. Its stacks and steps run every command through it,
    and the install's log and result both read what it ran."""

    clock: InstallClock = field(default_factory=InstallClock)
    workers: dict[InterpreterWorkerKey, InterpreterWorker] = field(default_factory=dict, init=False)
    worker_lock: Lock = field(default_factory=Lock, init=False)
    launcher: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="install-launch"),
        init=False,
    )

    def __enter__(self) -> InstallCommands:
        return self

    def close_workers(self, keys: tuple[InterpreterWorkerKey, ...]) -> None:
        """Every selected worker closes, even when another one's close raises. All of them
        finish before the first is reaped, so their interpreters exit together and the
        close waits for the slowest exit, not the sum of them."""
        with self.worker_lock, ExitStack() as exits:
            closing = tuple(self.workers.pop(key) for key in keys)
            for worker in closing:
                exits.callback(worker.close)
            for worker in closing:
                worker.finish()

    def close_runtime(self, python: Path) -> None:
        with WorkerClosePhase(str(python)).running(self.clock):
            self.close_workers(tuple(key for key in self.workers if key.python == python))

    def close(self) -> None:
        with WorkerClosePhase().running(self.clock):
            try:
                self.close_workers(tuple(self.workers))
            finally:
                self.launcher.shutdown(wait=True)

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def timing(self, phase: InstallPhase) -> Iterator[PhaseTiming]:
        print("+", phase.label(), file=sys.stderr, flush=True)
        with phase.running(self.clock) as measured:
            yield measured
        print("=", measured.line(), file=sys.stderr, flush=True)

    def run(self, *command: str | Path, cwd: Path = Path(),
            env: Mapping[str, str] = os.environ, label: str = "") -> str:
        with self.timing(CommandPhase(label or " ".join(map(str, command)))):
            completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                                       capture_output=True, text=True)
        if completed.returncode:
            raise UpgradeRefused(f"{command[0]} failed: {completed.stdout.strip()}\n"
                                 f"{completed.stderr.strip()}")
        return completed.stdout

    def git(self, checkout: Path, *args: str) -> str:
        """Git through the one runner (``Repo.git``), timed like every install command."""
        with self.timing(GitPhase(f"git -C {checkout} {' '.join(args)}")):
            return Repo.git(checkout, *args)

    def probe[Answer](self, probe: RuntimeProbe[Answer], python: Path, *,
                      cwd: Path = Path(), env: Mapping[str, str] = os.environ,
                      program: ProbeProgram = SourceProgram()) -> Answer:
        key = InterpreterWorkerKey(python, tuple(map(str, program.entry())), cwd.absolute(),
                                   tuple(sorted(MaintenanceHolder.outside(env).items())))
        with self.timing(probe.install_phase()) as measured:
            with self.worker_lock:
                if key not in self.workers:
                    # Linux binds a child's lifetime to its launching thread. Keep that
                    # thread until all workers close, including calls from short-lived pools.
                    self.workers[key] = self.launcher.submit(InterpreterWorker.start, key).result()
                worker = self.workers[key]
            try:
                answer = worker.exchange(FieldCodec.compact(FieldCodec.encode(
                    ProbeRequest(probe, MaintenanceHolder.current().nonce))))
            except InterpreterWorkerDied:
                self.close_workers((key,))
                raise
            reply = FieldCodec.decode(ProbeReply, FieldCodec.parse(answer))
            self.clock.include(reply.phases, measured)
            return FieldCodec.decode(probe.answer_type(), reply.answer)

