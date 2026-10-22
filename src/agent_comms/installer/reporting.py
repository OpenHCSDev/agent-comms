"""An install's root-writing permission and terminal outcome."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from traceback import FrameSummary, extract_tb
from typing import TYPE_CHECKING, cast

from agent_comms.bus_publication import AgentOrigin
from agent_comms.comms import Comms, client_wire
from agent_comms.declared_family import DeclaredFamily
from agent_comms.installer.backup import UpgradeRefused
from agent_comms.messages import InfoKind, Message
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.publisher import InitialWrites
from agent_comms.repo import Repo

if TYPE_CHECKING:
    from agent_comms.installer.launch import InstallReport
    from agent_comms.installer.upgrade import CarriedPass, Stack, Upgrade


class RootWriter(DeclaredFamily, ABC, affix="Writer"):
    """The runtime permitted to write this stage of an install's root."""

    def prepare(self, report: InstallReport) -> None:
        """Existing and carried roots are already initialized."""

    def post(self, root: Path, report: InstallReport, lines: tuple[str, ...]) -> None:
        self.write(root, report, "\n".join(lines))

    @abstractmethod
    def write(self, root: Path, report: InstallReport, body: str) -> None: ...

    @staticmethod
    def publish(comms: Comms, report: InstallReport, body: str) -> None:
        report.register(comms.registry)
        publisher = cast(InitialWrites, comms.bus.publisher)
        publisher.publish_ordinary(
            Message(
                sender=report.sender,
                target=report.target,
                body=body,
                type=InfoKind,
            ),
            origin=AgentOrigin(),
        )


@dataclass(frozen=True)
class InstalledWriter(RootWriter):
    stack: Stack

    def write(self, root: Path, report: InstallReport, body: str) -> None:
        self.stack.cli(root, "register", "--name", report.sender, "--worktree", str(root))
        self.stack.cli(root, "send", "--from", report.sender, "--to", report.target, "--body", body)


@dataclass(frozen=True)
class SourceWriter:
    """Shared source-side I/O; only the nominal RootWriter cases grant permission."""

    root: Path

    def write(self, root: Path, report: InstallReport, body: str) -> None:
        if root != self.root:
            raise ValueError("Install writer belongs to a different root")
        with client_wire(self.root) as comms:
            RootWriter.publish(comms, report, body)


class CarriedWriter(SourceWriter, RootWriter):
    """Constructed only by the pass which read the carried root back."""


class FreshWriter(SourceWriter, RootWriter):
    """The creation policy selected this empty root before any source-side I/O."""

    def prepare(self, report: InstallReport) -> None:
        comms = Comms(self.root)
        root_id = comms.messaging.initialize_private_initial_protocol()
        comms.channels.create_tag(report.channel)
        report.writing(PreparedFreshWriter(comms, root_id))


@dataclass(frozen=True)
class PreparedFreshWriter(RootWriter):
    comms: Comms
    root_id: str

    def write(self, root: Path, report: InstallReport, body: str) -> None:
        if root != self.comms.root:
            raise ValueError("Fresh install writer belongs to a different root")
        self.publish(self.comms, report, body)


@dataclass(frozen=True)
class CarriedInstallError(BaseException):
    """Completion failed after publication; the root did not roll back."""

    passed: CarriedPass
    error: BaseException


@dataclass(frozen=True)
class InstallOutcome(ABC):
    writer: RootWriter

    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...


class UnfinishedInstall(InstallOutcome):
    def lines(self) -> tuple[str, ...]:
        raise RuntimeError("Install exited without producing its result")


@dataclass(frozen=True)
class FinishedInstall(InstallOutcome):
    upgrade: Upgrade

    def lines(self) -> tuple[str, ...]:
        return ("Install finished", *self.upgrade.lines())


@dataclass(frozen=True)
class InstallFailure[Error: BaseException](
    InstallOutcome, DeclaredFamily, MroDispatch, affix="Install"
):
    error: Error

    @classmethod
    def of(
        cls,
        error: BaseException,
        writer: RootWriter,
        source: Repo,
    ) -> InstallFailure[BaseException]:
        handler = next(cls.class_handlers_for(type(error)))
        return cast(InstallFailure[BaseException], handler(error, writer, source))

    @handles(UpgradeRefused)
    @staticmethod
    def refused(error: UpgradeRefused, writer: RootWriter, source: Repo) -> RefusedInstall:
        return RefusedInstall(writer, error)

    @handles(CarriedInstallError)
    @classmethod
    def carried(
        cls,
        error: CarriedInstallError,
        writer: RootWriter,
        source: Repo,
    ) -> InstallFailure[BaseException]:
        return cls.of(error.error, error.passed.writer(), source)

    @handles(BaseException)
    @staticmethod
    def crashed(error: BaseException, writer: RootWriter, source: Repo) -> CrashedInstall:
        # The context supplies a Core frame even if the failing library is external.
        # Its package directory works in both a checkout and a built wheel.
        package = Path(__file__).resolve().parents[1]
        frame = next(
            frame
            for frame in reversed(extract_tb(error.__traceback__))
            if Path(frame.filename).resolve().is_relative_to(package)
        )
        module = ".".join(
            Path(frame.filename).resolve().relative_to(package.parent).with_suffix("").parts
        )
        location = FrameSummary(
            str(source.module_file(module)),
            frame.lineno,
            frame.name,
            lookup_line=False,
        )
        return CrashedInstall(writer, error, location)


class RefusedInstall(InstallFailure[UpgradeRefused]):
    def lines(self) -> tuple[str, ...]:
        return self.error.lines()


@dataclass(frozen=True)
class CrashedInstall(InstallFailure[BaseException]):
    frame: FrameSummary

    def lines(self) -> tuple[str, ...]:
        return (
            f"Install failed: {type(self.error).__name__}: {self.error}",
            f"{self.frame.filename}:{self.frame.lineno}",
        )
