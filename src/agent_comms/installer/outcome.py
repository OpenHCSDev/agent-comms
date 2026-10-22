"""An install's result; reportable exceptions participate directly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from traceback import FrameSummary
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_comms.installer.upgrade import CarriedPass, LiveRun, Upgrade


@dataclass(frozen=True)
class CarriedInstallError(BaseException):
    """Completion failed after publication; the root did not roll back."""

    passed: CarriedPass
    error: BaseException


class InstallOutcome(ABC):
    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...

    @abstractmethod
    def live_run(self) -> LiveRun: ...


@dataclass(frozen=True)
class ObservedInstall(InstallOutcome):
    """The live observation while no complete Upgrade owns it."""

    live: LiveRun

    def live_run(self) -> LiveRun:
        return self.live


class UnfinishedInstall(ObservedInstall):
    def lines(self) -> tuple[str, ...]:
        raise RuntimeError("Install exited without producing its result")


@dataclass(frozen=True)
class FinishedInstall(InstallOutcome):
    upgrade: Upgrade

    def lines(self) -> tuple[str, ...]:
        return ("Install finished", *self.upgrade.lines())

    def live_run(self) -> LiveRun:
        return self.upgrade.live_run()


class InstallFailure(ABC):
    """An error which supplies its terminal report and original exception."""

    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...

    @abstractmethod
    def exception(self) -> BaseException: ...


@dataclass(frozen=True)
class FailedInstall(ObservedInstall):
    failure: InstallFailure

    def lines(self) -> tuple[str, ...]:
        return (*self.failure.lines(), *self.live.bridge_lines())


@dataclass(frozen=True)
class CrashedInstall(InstallFailure):
    error: BaseException
    frame: FrameSummary

    def exception(self) -> BaseException:
        return self.error

    def lines(self) -> tuple[str, ...]:
        return (
            f"Install failed: {type(self.error).__name__}: {self.error}",
            f"{self.frame.filename}:{self.frame.lineno}",
        )
