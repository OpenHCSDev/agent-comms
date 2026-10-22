"""An install's permission to write its root at each schema stage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent_comms.bus_publication import AgentOrigin
from agent_comms.comms import Comms, client_wire
from agent_comms.declared_family import DeclaredFamily
from agent_comms.messages import InfoKind, Message
from agent_comms.publisher import InitialWrites

if TYPE_CHECKING:
    from agent_comms.installer.launch import InstallReport
    from agent_comms.installer.upgrade import Stack


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
