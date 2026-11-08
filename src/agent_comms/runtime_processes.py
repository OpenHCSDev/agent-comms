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
    def held_executable(self) -> frozenset[Path]:
        """What this process is running, for whoever retires the runtimes nothing runs.

        The executable and not a runtime: a bundled native child lives several directories
        inside its runtime, so which build owns it is a question for whoever holds the
        runtimes directory, and answering a prefix here would answer the wrong one.
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

    def held_executable(self) -> frozenset[Path]:
        """A process whose command could not be read names nothing; `unread` is how it
        keeps a sweep from deciding about the runtime it might have been running."""
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return (self.line(),)


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

    def held_executable(self) -> frozenset[Path]:
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

    def held_executables(self) -> frozenset[Path]:
        """Every command this snapshot found a process running out of a runtime."""
        return frozenset().union(*(outcome.held_executable() for outcome in self.outcomes),
                                 frozenset())

    def unread(self) -> tuple[str, ...]:
        """Every process this snapshot could not read."""
        return tuple(line for outcome in self.outcomes for line in outcome.unread())

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
            # Other namespaces do not name an agent-comms installation.
            interpreter = Path(invoked)
            if not interpreter.is_relative_to(directory):
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
