"""Publication belongs to the runtime that currently owns the install's bus."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.cli_commands import ReplaceRouteCliCommand
from agent_comms.installer.interpreter_operation import InstallReportProbe, ProbeProgram

if TYPE_CHECKING:
    from agent_comms.installer.upgrade import RehearsedUpgrade, Stack


class InstallOutcome(ABC):
    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...


class UnfinishedInstall(InstallOutcome):
    def lines(self) -> tuple[str, ...]:
        raise RuntimeError("Install exited without producing its result")


@dataclass(frozen=True)
class FinishedInstall(InstallOutcome):
    result: RehearsedUpgrade

    def lines(self) -> tuple[str, ...]:
        return (self.result.finished, *self.result.lines())


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
    program: ProbeProgram

    def post(self, lines: tuple[str, ...]) -> None:
        self.stack.probe(InstallReportProbe(self.root, "\n".join(lines)), program=self.program)


@dataclass(frozen=True)
class OutgoingWriter(RuntimeWriter):
    """The outgoing runtime posts until the target's runtime takes the bus over. It refuses
    a route that selects no bus, which install replaces anyway, so the target's runtime
    replaces that route before each post."""

    target: Stack

    def post(self, lines: tuple[str, ...]) -> None:
        self.target.cli(self.root, ReplaceRouteCliCommand)
        super().post(lines)
