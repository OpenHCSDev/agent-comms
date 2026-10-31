"""Publication belongs to the runtime that currently owns the install's bus."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING


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


class RootWriter(ABC):
    @abstractmethod
    def post(self, lines: tuple[str, ...]) -> None: ...


class UnpostedWriter(RootWriter):
    """Rehearsals and roots that do not yet exist have no publication destination."""

    def post(self, lines: tuple[str, ...]) -> None:
        pass


