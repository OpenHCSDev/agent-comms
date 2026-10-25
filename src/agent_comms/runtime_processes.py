"""Report old-runtime applications without acquiring authority to stop them."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.child_process import IdentityMismatchError, Platform, ProcessIdentity
from agent_comms.declared_family import DeclaredFamily
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.runtime_children import ApplicationChildInitiator


class RuntimeProcessOutcome(DeclaredFamily, ABC, affix="RuntimeProcess"):
    @abstractmethod
    def line(self) -> str: ...


@dataclass(frozen=True)
class UnreadableRuntimeProcess(RuntimeProcessOutcome):
    pid: int
    reason: str

    def line(self) -> str:
        return f"Unknown runtime PID {self.pid}: {self.reason}"


@dataclass(frozen=True)
class RuntimeApplication(ApplicationChildInitiator):
    """The observed parent application's exact identity and display name."""

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


@dataclass(frozen=True)
class RuntimeProcesses:
    """A read-only snapshot, not a process handle or permission to signal an app.

    Installer-launched children retain their own ChildProcess stop/reap authority.
    Discovering another application's interpreter grants no such authority.
    """

    outcomes: tuple[RuntimeProcessOutcome, ...]

    def lines(self) -> tuple[str, ...]:
        return tuple(outcome.line() for outcome in self.outcomes)

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
