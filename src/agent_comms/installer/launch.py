"""One install, run from the target revision's own source in a session of its own.

The installed CLI only fetches, resolves the target and checks its tree out; the install
itself runs that tree's ``src`` (``python -m agent_comms.installer``), so its carries,
rehearsal and harness setup are the target's, never the outgoing runtime's.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, ClassVar, TextIO

import agent_comms.installer
from agent_comms.change_report import FileLine
from agent_comms.cli_commands import InstallCliCommand, InstallResult
from agent_comms.comms import client_wire
from agent_comms.bus_publication import AgentOrigin
from agent_comms.field_codec import FieldCodec, Id128Text, PathText
from agent_comms.installer.backup import UpgradeRefused
from agent_comms.installer.upgrade import TargetTree, Upgrade, run, upgrade
from agent_comms.messages import InfoKind, Message
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.service_sender import ServiceSender
from agent_comms.storage_rules import StorageRule


class InstallOutcome(ABC):
    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...


class UnfinishedInstall(InstallOutcome):
    def lines(self) -> tuple[str, ...]:
        raise RuntimeError("Install exited without returning its result")


@dataclass(frozen=True)
class FinishedInstall(InstallOutcome):
    upgrade: Upgrade

    def lines(self) -> tuple[str, ...]:
        return ("Install finished", *self.upgrade.lines())


class InstallFailure(InstallOutcome, MroDispatch):
    """Decode a raised exception once; its member owns the result's presentation."""

    @classmethod
    def of(cls, error: BaseException) -> InstallFailure:
        handler = getattr(cls, next(cls.handler_names_for(type(error))))
        return handler(error)

    @handles(UpgradeRefused)
    @staticmethod
    def refused(error: UpgradeRefused) -> InstallFailure:
        return RefusedInstall(error)

    @handles(BaseException)
    @staticmethod
    def crashed(error: BaseException) -> InstallFailure:
        source = Path(__file__).parents[2]
        frame = next(frame for frame in reversed(traceback.extract_tb(error.__traceback__))
                     if Path(frame.filename).is_relative_to(source))
        return CrashedInstall(type(error).__qualname__, str(error),
                              FileLine(str(Path(frame.filename).relative_to(source.parent)),
                                       frame.lineno))


@dataclass(frozen=True)
class RefusedInstall(InstallFailure):
    error: UpgradeRefused

    def lines(self) -> tuple[str, ...]:
        return self.error.lines()


@dataclass(frozen=True)
class CrashedInstall(InstallFailure):
    exception_type: str
    message: str
    source: FileLine

    def lines(self) -> tuple[str, ...]:
        return (f"Install failed: {self.exception_type}: {self.message}",
                f"Source: {self.source.to_wire()}")


@dataclass
class InstallReport(ServiceSender):
    """One run owns its start and terminal post, including rehearsal and cleanup."""

    sender: ClassVar[str] = "installer"
    root: Path
    outcome: InstallOutcome = field(default_factory=UnfinishedInstall, init=False)

    def post(self, lines: tuple[str, ...]) -> Message:
        with client_wire(self.root) as comms:
            self.register(comms.registry)
            return comms.bus.publisher.publish_ordinary(
                Message(sender=self.sender, target="#comms", body="\n".join(lines),
                        type=InfoKind), origin=AgentOrigin())

    @contextmanager
    def running(self, upgrade: TargetTree) -> Iterator[InstallReport]:
        self.outcome = UnfinishedInstall()
        try:
            with upgrade:
                self.post(("Install starting", f"Target: {upgrade.revision[:12]}"))
                yield self
            self.outcome.lines()  # A missing result becomes a reported failure too.
        except BaseException as error:
            self.outcome = InstallFailure.of(error)
            raise
        finally:
            self.post(self.outcome.lines())

    def complete(self, upgrade: Upgrade) -> Upgrade:
        self.outcome = FinishedInstall(upgrade)
        return upgrade


@dataclass(frozen=True)
class InstallRun:
    command: InstallCliCommand
    root: Annotated[Path, PathText]
    scratch: Annotated[Path, PathText]
    target: str

    @classmethod
    def start(cls, command: InstallCliCommand, root: Path) -> InstallRun:
        checkout = command.checkout
        run("git", "-C", checkout, "fetch", "-q", "origin")
        target = run("git", "-C", checkout, "rev-parse", "--verify",
                     f"{command.to}^{{commit}}").strip()
        parent = command.installer.scratch(StorageRule.load(root))
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(dir=parent, prefix="install-"))
        TargetTree(checkout, scratch, target).add()
        return cls(command, root, scratch, target)

    def launch(self, output: TextIO) -> int:
        """The install outlives its caller: a new session, its output in files beside the bus.
        Retiring the caller's process group must not cut an install short."""
        tree = TargetTree(self.command.checkout, self.scratch, self.target)
        logs = self.root.parent / f".{self.root.name}.installer-{Id128Text.mint()}"
        logs.mkdir(mode=0o700)
        result, errors = logs / "result.json", logs / "stderr.log"
        with result.open("xb") as stdout, errors.open("xb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-m", agent_comms.installer.__name__], cwd=tree.path,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, tree.source_paths()))},
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True,
            )
        print(f"install: pid {process.pid} runs {self.target[:12]}; diagnostics {errors}",
              file=sys.stderr, flush=True)
        process.communicate(json.dumps(FieldCodec.encode(self)).encode())
        if process.returncode:
            print(f"install failed ({process.returncode}): {errors.read_text().strip()}",
                  file=sys.stderr)
        else:
            output.write(result.read_text())
        return process.returncode

    def run(self) -> Upgrade:
        command = self.command
        return upgrade(command.checkout, self.root, self.target, command.installer,
                       command.rehearse, command.retire, command.spill_dir, self.scratch,
                       self.harnesses, InstallReport(self.root))

    def harnesses(self) -> InstallResult:
        with client_wire(self.root) as comms:
            return self.command.apply(comms)
