"""Publication belongs to the runtime that currently owns the install's bus."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.installer.probes.report import InstallReportProbe
from agent_comms.installer.refusals import InterpreterWorkerDied

if TYPE_CHECKING:
    from agent_comms.installer.interpreter_operation import PhaseTiming


class InstallOutcome(ABC):
    @abstractmethod
    def lines(self, inherited: tuple[PhaseTiming, ...] = ()) -> tuple[str, ...]: ...

    @abstractmethod
    def live_lines(self) -> tuple[str, ...]: ...


class UnfinishedInstall(InstallOutcome):
    def lines(self, inherited: tuple[PhaseTiming, ...] = ()) -> tuple[str, ...]:
        raise RuntimeError("Install exited without producing its result")

    def live_lines(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class FailedInstall(InstallOutcome):
    """Failure retains the result already observed, without replacing its error."""

    previous: InstallOutcome
    failure: tuple[str, ...]

    def lines(self, inherited: tuple[PhaseTiming, ...] = ()) -> tuple[str, ...]:
        return (*self.failure, *self.live_lines())

    def live_lines(self) -> tuple[str, ...]:
        return self.previous.live_lines()


class ReportPublication:
    """The result of an announcement, never permission to change the runtime."""

    def lines(self) -> tuple[str, ...]:
        return ()


class UnattemptedPublication(ReportPublication):
    """No publication destination was selected."""


class ConfirmedPublication(ReportPublication):
    """The selected writer returned after the publication."""


@dataclass(frozen=True)
class UnconfirmedPublication(ReportPublication):
    """The worker's exit does not establish whether its write committed."""

    failure: InterpreterWorkerDied

    def lines(self) -> tuple[str, ...]:
        return (f"Starting report unconfirmed; not retried: {self.failure}",)


class RootWriter(ABC):
    def starting(self, lines: tuple[str, ...]) -> ReportPublication:
        """An uncertain announcement is reported, not retried or used for takeover.

        Retirement and carry still execute through their original runtime. If that
        runtime cannot perform those operations, their own failure stops the install.
        """
        try:
            self.post(lines)
        except InterpreterWorkerDied as failure:
            return UnconfirmedPublication(failure)
        return ConfirmedPublication()

    @abstractmethod
    def post(self, lines: tuple[str, ...]) -> None: ...


class UnpostedWriter(RootWriter):
    """Rehearsals and roots that do not yet exist have no publication destination."""

    def starting(self, lines: tuple[str, ...]) -> ReportPublication:
        return UnattemptedPublication()

    def post(self, lines: tuple[str, ...]) -> None:
        pass


