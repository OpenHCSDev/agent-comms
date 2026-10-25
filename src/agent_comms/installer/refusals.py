"""Install refusals, independent of installer orchestration."""

from __future__ import annotations

from typing import ClassVar

from agent_comms.child_process import ChildOutcome
from agent_comms.coordination_errors import CoordinationError
from agent_comms.launch_runtime import LaunchRuntime


class UpgradeRefused(CoordinationError):
    """The update stopped without permission to discard its original data."""

    prefix: ClassVar[str] = "install: refused:"

    def __str__(self) -> str:
        return f"{self.prefix} {super().__str__()}"

    def lines(self) -> tuple[str, ...]:
        return (f"Install failed: {self}",)


class RuntimeWireChanged(UpgradeRefused):
    """The outgoing runtime cannot decode the target's cross-runtime declarations."""

    def __init__(self, commit: str, drift: tuple[str, ...]) -> None:
        super().__init__(f"Installed Core {commit} has incompatible runtime wire: " + "; ".join(drift))


class InterpreterWorkerDied(UpgradeRefused):
    """A worker ends only when an operation raised in it; its exit and its stderr, which
    holds that traceback, are the failure."""

    def __init__(self, outcome: ChildOutcome, errors: str) -> None:
        super().__init__(f"Interpreter worker {outcome.summary} without an answer: {errors}")
        self.outcome = outcome
        self.errors = errors


class InstallRolledBack(UpgradeRefused):
    """The original installation was restored after a failed live pass."""

    def __init__(
        self,
        error: BaseException,
        backup: str,
        *,
        runtime: LaunchRuntime,
    ):
        self.error = error
        self.backup = backup
        self.runtime = runtime
        super().__init__("; ".join(self.lines()))

    def lines(self) -> tuple[str, ...]:
        return (
            f"Install rolled back: {type(self.error).__name__}: {self.error}",
            f"Original runtime selected again: {self.runtime.prefix}",
            f"Backup: {self.backup}",
        )
