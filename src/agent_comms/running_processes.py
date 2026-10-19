"""The processes this user runs, read once from /proc, and how Core ends one exactly."""

from __future__ import annotations

import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .child_process import LinuxPlatform, ProcessIdentity, ProcessState


@dataclass(frozen=True)
class RunningProcess:
    """One /proc entry: its exact identity, its parent, its command and the executables it started
    from.

    A venv's bin/python resolves to the base interpreter, so the launched argv[0] counts with only
    its directory resolved; /proc/<pid>/exe counts when this user may read it."""

    identity: ProcessIdentity
    parent: int
    command: str
    executables: tuple[Path, ...]

    @classmethod
    def read(cls, entry: Path) -> RunningProcess:
        # Fields after comm's final ')' start at field 3: ppid is 4, pgrp 5, starttime 22.
        fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            raise ProcessLookupError(entry.name)
        argv = (entry / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        argv0 = Path(os.fsdecode(argv[0]))
        launched: tuple[Path, ...] = (
            (argv0.parent.resolve() / argv0.name,) if argv0.is_absolute() else ()
        )
        with suppress(PermissionError):
            launched += (Path(os.readlink(entry / "exe")),)
        identity = ProcessIdentity.in_group(int(entry.name), int(fields[19]), int(fields[2]))
        return cls(identity, int(fields[1]), " ".join(map(os.fsdecode, argv)), launched)

    def runs_from(self, runtime: Path) -> bool:
        return any(path.is_relative_to(runtime) for path in self.executables)

    def runtimes(self) -> tuple[Path, ...]:
        """The agent-comms runtimes it started from: each a bin/ directory holding agent-comms."""
        return tuple(
            path.parent.parent
            for path in self.executables
            if (path.parent / "agent-comms").is_file()
        )


@dataclass(frozen=True)
class ProcessTable:
    """Every process this user may read, keyed by PID, from one read of /proc."""

    running: dict[int, RunningProcess]
    grace: float = 5.0

    @classmethod
    def read(cls) -> ProcessTable:
        """A process that exits during the read is left out."""
        running = {}
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                with suppress(FileNotFoundError, ProcessLookupError):
                    running[int(entry.name)] = RunningProcess.read(entry)
        return cls(running)

    def lineage(self, pid: int) -> tuple[ProcessIdentity, ...]:
        """The process and each ancestor still running, nearest first."""
        line: dict[int, ProcessIdentity] = {}
        while pid in self.running and pid not in line:
            line[pid] = self.running[pid].identity
            pid = self.running[pid].parent
        return tuple(line.values())

    def held(self, process: RunningProcess, holders: tuple[ProcessState, ...]) -> bool:
        """Whether a holder's process is this one or one of its ancestors."""
        return any(identity in holders for identity in self.lineage(process.identity.pid))

    def reader(self) -> tuple[ProcessIdentity, ...]:
        """This process and its ancestors: what reads the table never ends itself."""
        return self.lineage(os.getpid())

    def end(self, processes: tuple[RunningProcess, ...]) -> tuple[RunningProcess, ...]:
        """SIGTERM each exact process, one shared wait, then SIGKILL any still running."""
        platform = LinuxPlatform()
        signalled: list[tuple[RunningProcess, int]] = []
        for process in processes:
            with suppress(ProcessLookupError):
                descriptor = platform.open_pidfd(process.identity.pid)
                try:
                    platform.require(process.identity)
                    platform.signal_pidfd(descriptor, signal.SIGTERM)
                except ProcessLookupError:
                    os.close(descriptor)
                    continue
                signalled.append((process, descriptor))
        deadline = time.monotonic() + self.grace
        for _, descriptor in signalled:
            try:
                platform.watch_deadline(descriptor, deadline)
            finally:
                os.close(descriptor)
        return tuple(process for process, _ in signalled)
