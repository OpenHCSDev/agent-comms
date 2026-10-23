"""Publication belongs to the runtime that currently owns the install's bus."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.installer.probes.report import InstallReportProbe

if TYPE_CHECKING:
    from agent_comms.installer.interpreter_operation import PhaseTiming
    from agent_comms.installer.upgrade import LiveRun, RehearsedUpgrade, Stack


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
class ObservedInstall(UnfinishedInstall):
    """A live result returned before harness registration or cleanup can fail."""

    live: LiveRun

    def live_lines(self) -> tuple[str, ...]:
        return self.live.lines()


@dataclass(frozen=True)
class FinishedInstall(InstallOutcome):
    result: RehearsedUpgrade

    def lines(self, inherited: tuple[PhaseTiming, ...] = ()) -> tuple[str, ...]:
        result = replace(self.result, phases=[*inherited, *self.result.phases])
        return (result.finished, *result.lines())

    def live_lines(self) -> tuple[str, ...]:
        return self.result.live_lines()


@dataclass(frozen=True)
class FailedInstall(InstallOutcome):
    """Failure retains the result already observed, without replacing its error."""

    previous: InstallOutcome
    failure: tuple[str, ...]

    def lines(self, inherited: tuple[PhaseTiming, ...] = ()) -> tuple[str, ...]:
        return (*self.failure, *self.live_lines())

    def live_lines(self) -> tuple[str, ...]:
        return self.previous.live_lines()


class RootWriter(ABC):
    @abstractmethod
    def post(self, lines: tuple[str, ...]) -> None: ...


class UnpostedWriter(RootWriter):
    """Rehearsals and roots that do not yet exist have no publication destination."""

    def post(self, lines: tuple[str, ...]) -> None:
        pass


@dataclass(frozen=True)
class RuntimeWriter(RootWriter):
    root: Path
    stack: Stack

    def post(self, lines: tuple[str, ...]) -> None:
        self.stack.probe(InstallReportProbe(self.root, "\n".join(lines)))
