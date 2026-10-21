"""Observe and retire exact processes from the target installation's runtime directory."""

from __future__ import annotations

import asyncio
import os
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.child_process import ProcessIdentity
from agent_comms.declared_family import DeclaredFamily
from agent_comms.launch_runtime import LaunchRuntime


class RuntimeRetirement(DeclaredFamily, ABC, affix="RuntimeRetirement"):
    """A reported stop is established by its process observation, never its signal."""

    successful: ClassVar[bool] = False

    @abstractmethod
    def line(self) -> str: ...


class RuntimeCandidate(ABC):
    @abstractmethod
    async def retire(self, seconds: float) -> RuntimeRetirement: ...


@dataclass(frozen=True)
class UnreadableRuntimeProcess(RuntimeCandidate, RuntimeRetirement):
    pid: int
    reason: str

    async def retire(self, seconds: float) -> RuntimeRetirement:
        return self

    def line(self) -> str:
        return f"Unknown runtime PID {self.pid}: {self.reason}"


@dataclass(frozen=True, eq=False)
class RuntimeProcess(ProcessIdentity, LaunchRuntime, RuntimeCandidate, declared_name=None):
    """The runtime and PID/start-time identity observed in one process lifetime."""

    async def retire(self, seconds: float) -> RuntimeRetirement:
        if self.runs_here():
            return RunningRuntimeProcess(
                self, "installer must transfer to the target runtime before retirement"
            )
        try:
            self.signal(signal.SIGTERM)
            async with asyncio.timeout(seconds):
                await self.wait_exited()
        except ProcessLookupError, FileNotFoundError:
            # The observed incarnation ended; a reused PID is not ours to signal.
            pass
        except TimeoutError:
            return RunningRuntimeProcess(self, "did not exit after SIGTERM")
        except PermissionError as error:
            return RunningRuntimeProcess(self, str(error))
        return StoppedRuntimeProcess(self)


@dataclass(frozen=True)
class KnownRuntimeRetirement(RuntimeRetirement):
    process: RuntimeProcess


class StoppedRuntimeProcess(KnownRuntimeRetirement):
    successful = True

    def line(self) -> str:
        return f"Stopped old-runtime PID {self.process.pid}: {self.process.prefix}"


@dataclass(frozen=True)
class RunningRuntimeProcess(KnownRuntimeRetirement):
    reason: str

    def line(self) -> str:
        return (
            f"Still alive old-runtime PID {self.process.pid}: "
            f"{self.process.prefix}; {self.reason}"
        )


@dataclass(frozen=True)
class RuntimeProcesses:
    """One bounded process snapshot; no executable-name roster or resolved ELF lookup."""

    candidates: tuple[RuntimeCandidate, ...]

    @classmethod
    def observe(cls, target: LaunchRuntime, proc: Path = Path("/proc")) -> RuntimeProcesses:
        directory = target.prefix.parent
        candidates: list[RuntimeCandidate] = []
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                identity = ProcessIdentity.capture(pid)
                invoked = (entry / "cmdline").read_bytes().partition(b"\0")[0]
                identity.require_alive()
            except ProcessLookupError, FileNotFoundError:
                continue
            except OSError as error:
                candidates.append(UnreadableRuntimeProcess(pid, str(error)))
                continue
            # Empty command lines declare no userspace invocation (kernel/exited
            # tasks). Other namespaces do not name an agent-comms installation.
            interpreter = Path(os.fsdecode(invoked))
            if not invoked or not interpreter.is_relative_to(directory):
                continue
            runtime = LaunchRuntime.from_interpreter(interpreter)
            if runtime.prefix != target.prefix:
                candidates.append(
                    RuntimeProcess(
                        pid=identity.pid,
                        start_time=identity.start_time,
                        interpreter=runtime.interpreter,
                    )
                )
        return cls(tuple(candidates))

    async def retire(self, seconds: float) -> tuple[RuntimeRetirement, ...]:
        """Join the whole captured set; every unknown or live result remains a failure."""
        return tuple(
            await asyncio.gather(*(candidate.retire(seconds) for candidate in self.candidates))
        )
