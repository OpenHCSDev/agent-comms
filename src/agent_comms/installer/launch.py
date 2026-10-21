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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, ClassVar, TextIO

import agent_comms.installer
from agent_comms.cli_commands import InstallCliCommand, InstallResult
from agent_comms.comms import client_wire
from agent_comms.field_codec import FieldCodec, Id128Text, PathText
from agent_comms.installer.reporting import (
    FinishedInstall,
    InstalledWriter,
    InstallFailure,
    InstallOutcome,
    RootWriter,
    UnfinishedInstall,
)
from agent_comms.installer.upgrade import (
    CarriedPass,
    Install,
    Stack,
    TargetTree,
    Upgrade,
    run,
    upgrade,
)
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.service_sender import ServiceSender
from agent_comms.storage_rules import StorageRule


@dataclass
class InstallReport(ServiceSender, MroDispatch):
    """One run owns its writing permission and exactly one terminal result."""

    sender: ClassVar[str] = "installer"
    channel: ClassVar[str] = "comms"
    root: Path
    outgoing: Stack
    outcome: InstallOutcome = field(init=False)

    def __post_init__(self) -> None:
        self.outcome = UnfinishedInstall(InstalledWriter(self.outgoing))

    @classmethod
    def for_run(cls, request: InstallRun) -> InstallReport:
        return cls(request.root, Stack(request.command.checkout, runtime=LaunchRuntime.running()))

    @property
    def target(self) -> str:
        return f"#{self.channel}"

    def writing(self, writer: RootWriter) -> None:
        self.outcome = UnfinishedInstall(writer)
        writer.prepare(self)

    @handles(CarriedPass)
    def carried(self, passed: CarriedPass) -> None:
        if passed.root != self.root:
            raise ValueError("Carried pass belongs to a different install root")
        self.writing(passed.writer())

    def complete(self, result: Upgrade) -> Upgrade:
        self.outcome = FinishedInstall(self.outcome.writer, result)
        return result

    @contextmanager
    def running(self, request: InstallRun) -> Iterator[type[Install]]:
        try:
            with TargetTree.from_run(request):
                member = Install.member(self.root, request.command.retire)
                self.writing(member.initial_writer(self.root, self.outgoing))
                self.outcome.writer.post(self.root, self, (
                    "Install starting", f"Target: {request.target[:12]}",
                ))
                yield member
            # A caller cannot accidentally leave the context without its result.
            self.outcome.lines()
        except BaseException as error:
            failure = InstallFailure.of(error, self.outcome.writer)
            self.outcome = failure
            raise failure.error from failure.error.__cause__
        finally:
            self.outcome.writer.post(self.root, self, self.outcome.lines())


@dataclass(frozen=True)
class InstallRun:
    command: InstallCliCommand
    root: Annotated[Path, PathText]
    scratch: Annotated[Path, PathText]
    target: str

    @classmethod
    def start(cls, command: InstallCliCommand, root: Path) -> InstallRun:
        run("git", "-C", command.checkout, "fetch", "-q", "origin")
        target = run("git", "-C", command.checkout, "rev-parse", "--verify",
                     f"{command.to}^{{commit}}").strip()
        parent = command.installer.scratch(StorageRule.load(root))
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(dir=parent, prefix="install-"))
        request = cls(command, root, scratch, target)
        TargetTree.from_run(request).add()
        return request

    def launch(self, output: TextIO) -> int:
        """The install outlives its caller: a new session, its output in files beside the bus.
        Retiring the caller's process group must not cut an install short."""
        tree = TargetTree.from_run(self)
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
        return upgrade(self, InstallReport.for_run(self))

    def harnesses(self) -> InstallResult:
        with client_wire(self.root) as comms:
            return self.command.apply(comms)
