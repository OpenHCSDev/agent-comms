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


class OwnersNotRestarted(UpgradeRefused):
    """The install failed, then starting its stopped owners again failed too. The install's
    own failure is the cause; the refused restart only follows from it, so both are named
    (run 040380a8, 2026-10-30: the restart's "not retired yet" was the only line printed)."""

    def __init__(self, cause: BaseException, restart: UpgradeRefused) -> None:
        self.cause = cause
        self.restart = restart
        super().__init__(f"{type(cause).__name__}: {cause}; then restarting the stopped "
                         f"owners failed: {restart}")


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
