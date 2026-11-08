"""Report old-runtime applications without acquiring authority to stop them."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.child_process import IdentityMismatchError, Platform, ProcessIdentity
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.launch_runtime import LaunchRuntime


class RuntimeProcessOutcome(DeclaredFamily, ABC, affix="RuntimeProcess"):
    @abstractmethod
    def line(self) -> str: ...

    @abstractmethod
    def held_paths(self) -> frozenset[Path]:
        """What this process uses from inside the installation, for whoever retires the
        runtimes nothing uses: what it was invoked with, or what it can import.

        Paths and not runtimes: a bundled native child lives several directories inside
        its runtime and an import root is a directory within one, so which build owns a
        path is a question for whoever holds the runtimes directory. Answering a prefix
        here would answer the wrong one.
        """

    @abstractmethod
    def unread(self) -> tuple[str, ...]:
        """What this outcome could not read, which leaves a runtime undecided."""


@dataclass(frozen=True)
class UnreadableRuntimeProcess(RuntimeProcessOutcome):
    pid: int
    reason: str

    def line(self) -> str:
        return f"Unknown runtime PID {self.pid}: {self.reason}"

    def held_paths(self) -> frozenset[Path]:
        """A process whose command could not be read names nothing; `unread` is how it
        keeps a sweep from deciding about the runtime it might have been running."""
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return (self.line(),)


@dataclass(frozen=True)
class UnattributedRuntimeProcess(RuntimeProcessOutcome):
    """Invoked through the installation's moving pointer rather than a build.

    `current` names whichever runtime was published when the process started, and once it
    has moved there is nothing left in the command to say which that was. The process
    therefore holds a build and will not say which one, so a retirement must not decide
    about any of them — `unread` is how it refuses the whole sweep.

    Nothing on this machine was invoked this way when the retention was written; its own
    console scripts carry the built interpreter in their shebang, so a scan sees a build.
    It is a reachable invocation, not a hypothetical one: the installer's verification
    commands are written against `current/bin/python`.
    """

    pid: int
    executable: Path

    def line(self) -> str:
        return (f"Unattributed runtime PID {self.pid}: invoked as {self.executable}, "
                "which names the installation's pointer rather than a build")

    def held_paths(self) -> frozenset[Path]:
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return (self.line(),)


@dataclass(frozen=True)
class ImportingRuntimeProcess(RuntimeProcessOutcome):
    """A process with an import root inside the installation, whatever it was invoked with.

    An import root is not a PATH entry. PATH is resolution: one entry wins, and the winner
    is already visible as the command, which is why 47 historical runtimes inherited on one
    process's PATH hold nothing. Every entry on an import path stays live for as long as the
    process does, so a build a running process can import from at any moment is in use —
    codex-latency ran a system interpreter with `PYTHONPATH` into a build and watched the
    collector call that build reclaimable.

    An environment this cannot read belongs to another user, and the installation is this
    user's at 0700, so there is nothing there for them to import.
    """

    pid: int
    roots: tuple[Path, ...]

    def line(self) -> str:
        return (f"Importing-runtime PID {self.pid}: "
                f"{', '.join(str(root) for root in self.roots)}")

    def held_paths(self) -> frozenset[Path]:
        return frozenset(self.roots)

    def unread(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class RuntimeApplication:
    """The observed parent application's exact identity and display name."""

    process: ProcessIdentity
    name: str

    def describe(self) -> str:
        return f"{self.name} (application PID {self.process.pid})"


@dataclass(frozen=True, eq=False)
class RuntimeProcess(ProcessIdentity, LaunchRuntime, declared_name=None):
    application: RuntimeApplication


@dataclass(frozen=True)
class UnretiredRuntimeProcess(RuntimeProcessOutcome):
    process: RuntimeProcess

    def line(self) -> str:
        return (
            f"Unretired old-runtime PID {self.process.pid}: {self.process.prefix}; "
            f"owner {self.process.application.describe()}; not signalled"
        )

    def held_paths(self) -> frozenset[Path]:
        """What this process was invoked with — not what its inherited PATH would have
        found, which on one machine listed 47 runtimes for a single process."""
        return frozenset((self.process.interpreter,))

    def unread(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class RuntimeProcesses:
    """A read-only snapshot, not a process handle or permission to signal an app.

    Installer-launched children retain their own ChildProcess stop/reap authority.
    Discovering another application's interpreter grants no such authority.
    """

    outcomes: tuple[RuntimeProcessOutcome, ...]

    def lines(self) -> tuple[str, ...]:
        return tuple(outcome.line() for outcome in self.outcomes)

    def held_paths(self) -> frozenset[Path]:
        """Everything inside the installation this snapshot found a process using."""
        return frozenset().union(*(outcome.held_paths() for outcome in self.outcomes),
                                 frozenset())

    def unread(self) -> tuple[str, ...]:
        """Every process this snapshot could not read."""
        return tuple(line for outcome in self.outcomes for line in outcome.unread())

    @staticmethod
    def import_roots(platform: Platform, identity: ProcessIdentity,
                     directory: Path) -> frozenset[Path]:
        """The directories inside ``directory`` this process can import from.

        Read from its environment rather than guessed: `PYTHONPATH` entries and a
        `VIRTUAL_ENV` are the roots a live interpreter keeps on its import path, so a
        build named by one is in use however the process was invoked. An environment this
        cannot read belongs to another user, who cannot read the installation either.
        """
        try:
            environment = platform.environment(identity)
        except (ProcessLookupError, OSError, IdentityMismatchError):
            return frozenset()
        named = (*environment.get("PYTHONPATH", "").split(os.pathsep),
                 environment.get("VIRTUAL_ENV", ""))
        return frozenset(Path(value) for value in named
                         if value and Path(value).is_relative_to(directory))

    @classmethod
    def observe(cls, target: LaunchRuntime, proc: Path = Path("/proc")) -> RuntimeProcesses:
        directory = target.prefix.parent
        outcomes: list[RuntimeProcessOutcome] = []
        platform = Platform.current()
        observer = ProcessIdentity.capture(os.getpid())
        lineage: frozenset[ProcessIdentity] = frozenset(observer.lineage())
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                identity = ProcessIdentity.capture(pid)
                invoked = platform.arguments(identity)[0]
                identity.require_alive()
            except ProcessLookupError:
                continue
            except (OSError, IdentityMismatchError) as error:
                outcomes.append(UnreadableRuntimeProcess(pid, str(error)))
                continue
            # Independent of the command: a process importing from a build holds it even
            # when it was launched by a system interpreter from somewhere else entirely.
            roots = cls.import_roots(platform, identity, directory)
            if roots:
                outcomes.append(ImportingRuntimeProcess(pid, tuple(sorted(roots))))
            # Other namespaces do not name an agent-comms installation.
            interpreter = Path(invoked)
            if not interpreter.is_relative_to(directory):
                if interpreter.is_relative_to(LaunchRuntime.pointer_in(directory.parent)):
                    outcomes.append(UnattributedRuntimeProcess(pid, interpreter))
                continue
            runtime = LaunchRuntime.from_interpreter(interpreter)
            if runtime.prefix == target.prefix:
                continue
            try:
                if identity in lineage or observer in identity.lineage():
                    continue
                parent = platform.parent_identity(pid)
                application = RuntimeApplication(parent, platform.process_name(parent.pid))
                parent.require_alive()
                identity.require_alive()
            except (OSError, IdentityMismatchError) as error:
                outcomes.append(UnreadableRuntimeProcess(pid, str(error)))
                continue
            outcomes.append(
                UnretiredRuntimeProcess(
                    RuntimeProcess(
                        pid=identity.pid,
                        start_time=identity.start_time,
                        interpreter=runtime.interpreter,
                        application=application,
                    )
                )
            )
        return cls(tuple(outcomes))
