"""One install, run from the target revision's own source in a systemd unit of its own.

The installed CLI only fetches, resolves the target and checks its tree out; the install
itself runs that tree's ``src`` (``python -m agent_comms.installer``), so its carries,
rehearsal and harness setup are the target's, never the outgoing runtime's.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from traceback import extract_tb
from typing import Annotated, TextIO

import agent_comms.installer
from agent_comms.cli_bridges.shared import SessionBridgeResumes
from agent_comms.installer.request import InstallRequest
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.installer.logs import InstallLogs, InstallUnit
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import (
    FailureReportPublicationPhase,
    FinishedReportPublicationPhase,
    InstallClock,
    LaunchedPhaseTiming,
    LauncherPhase,
    PhaseTiming,
    TargetCheckoutPreparePhase,
    TreeClosePhase,
)
from agent_comms.installer.probes.harnesses import HarnessesProbe, InstallResult
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.reporting import (
    FailedInstall,
    FinishedInstall,
    InstallOutcome,
    ObservedInstall,
    RootWriter,
    UnfinishedInstall,
    UnpostedWriter,
)
from agent_comms.installer.runtime_resolution import CorePackage
from agent_comms.installer.upgrade import (
    LiveRun,
    OwnersStopped,
    RehearsedUpgrade,
    Stack,
    TargetTree,
    Upgrade,
    upgrade,
)
from agent_comms.repo import Repo
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.storage_rules import StorageRule
from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)


def stopped(signum: int, frame: object) -> None:
    """A stop runs the install's abort once; another signal cannot cut it short."""
    for each in (signal.SIGTERM, signal.SIGINT):
        signal.signal(each, signal.SIG_IGN)
    raise SystemExit(f"install: stopped by {signal.Signals(signum).name}")


@dataclass
class InstallReport(MroDispatch):
    """The operation owns one terminal report, after its resources have closed."""

    writer: RootWriter = field(default_factory=UnpostedWriter)
    outcome: InstallOutcome = field(default_factory=UnfinishedInstall, init=False)
    bridges: SessionBridgeResumes = field(default_factory=SessionBridgeResumes, init=False)
    owners: OwnersStopped = field(default_factory=OwnersStopped, init=False)
    inherited_phases: list[PhaseTiming] = field(default_factory=list)
    source: Repo = field(init=False, repr=False)

    def record_live(self, result: LiveRun) -> LiveRun:
        self.outcome = ObservedInstall(result)
        return result

    def complete(self, result: RehearsedUpgrade) -> RehearsedUpgrade:
        self.outcome = FinishedInstall(result)
        return result

    def starting(self, writer: RootWriter, target: str) -> None:
        self.writer = writer
        self.writer.post(("Install starting", f"Target: {target[:12]}"))

    @handles(UpgradeRefused)
    def refused(self, error: UpgradeRefused) -> tuple[str, ...]:
        return error.lines()

    @handles(BaseException)
    def crashed(self, error: BaseException) -> tuple[str, ...]:
        # The target tree is already removed here, so paths are compared as imported, never
        # resolved: a removed working directory makes even a non-strict resolve raise.
        package = Path(agent_comms.__file__).parent
        frame = next(
            frame for frame in reversed(extract_tb(error.__traceback__))
            if Path(frame.filename).is_relative_to(package)
        )
        module = ".".join(
            Path(frame.filename).relative_to(package.parent).with_suffix("").parts
        )
        location = self.source.module_file(module)
        return (f"Install failed: {type(error).__name__}: {error}",
                f"{location}:{frame.lineno}")

    @contextmanager
    def running(self, tree: TargetTree, clock: InstallClock) -> Iterator[None]:
        """Every writer runs installed artifacts from outside ``tree``, so the report follows
        the tree's cleanup and a cleanup failure is the install's failure."""
        clock.phases[:0] = self.inherited_phases
        self.source = Repo(checkout=tree.path, github=CorePackage.repository)
        try:
            tree.__enter__()
            try:
                yield
            finally:
                with TreeClosePhase().running(clock):
                    tree.__exit__(*sys.exc_info())
            lines = self.outcome.lines()
        except BaseException as error:
            self.outcome = FailedInstall(self.outcome, next(self.handlers_for(error))(error))
            try:
                with FailureReportPublicationPhase().running(clock):
                    self.writer.post((*self.outcome.lines(), *self.owners.lines(),
                                      *self.bridges.lines()))
            except BaseException as publication_error:
                error.add_note(f"Install failure report also failed: {publication_error}")
                print(f"install: failure report also failed: {publication_error}", file=sys.stderr)
            raise
        else:
            with FinishedReportPublicationPhase().running(clock):
                self.writer.post(lines)


@dataclass(frozen=True)
class InstallRun:
    command: InstallRequest
    root: Annotated[Path, PathText]
    scratch: Annotated[Path, PathText]
    target: str
    phases: list[LaunchedPhaseTiming] = field(default_factory=list)

    @classmethod
    def start(cls, command: InstallRequest, root: Path,
              launcher: LauncherPhase) -> InstallRun:
        checkout = command.checkout
        with InstallCommands() as commands, launcher.running(commands.clock):
            commands.git(checkout, "fetch", "-q", "origin")
            target = commands.git(checkout, "rev-parse", "--verify",
                                  f"{command.to}^{{commit}}").strip()
        parent = command.installer.scratch(StorageRule.load(root))
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(dir=parent, prefix="install-"))
        with TargetCheckoutPreparePhase().running(commands.clock):
            TargetTree(checkout, scratch, target).add()
        return cls(command, root, scratch, target,
                   [timing.launched() for timing in commands.clock.phases])

    @classmethod
    def read(cls, unit: InstallUnit) -> InstallRun:
        """The run a launched unit was handed, decoded from its request file."""
        return FieldCodec.decode(cls, FieldCodec.parse(unit.request.read_text()))

    @cached_property
    def unit(self) -> InstallUnit:
        return InstallLogs(self.root).minted()

    def spawn(self, *, wait: bool) -> None:
        """The install runs in its own unit and reads its request from a file beside the bus,
        so stopping the caller's unit, the connector's included, cannot cut it short."""
        tree = TargetTree(self.command.checkout, self.scratch, self.target)
        self.unit.request.write_text(json.dumps(FieldCodec.encode(self)))
        print(f"install: {self.unit.unit} runs {self.target[:12]}; "
              f"diagnostics {self.unit.errors}", file=sys.stderr, flush=True)
        self.unit.start(
            tree.path, (sys.executable, "-m", agent_comms.installer.__name__),
            os.pathsep.join(map(str, tree.source_paths())), wait=wait,
        )

    def launch(self, output: TextIO) -> int:
        try:
            self.spawn(wait=True)
        except subprocess.CalledProcessError as failed:
            print(f"install failed ({failed.returncode}): {self.unit.errors.read_text().strip()}",
                  file=sys.stderr)
            return failed.returncode
        output.write(self.unit.result.read_text())
        return 0

    def run(self, runtime: LaunchRuntime) -> Upgrade:
        command = self.command
        return upgrade(command.checkout, self.root, self.target, command.installer,
                       command.mode, command.retire, command.spill_dir, self.scratch,
                       runtime, self.harnesses, InstallReport(
                           inherited_phases=[launched.timing() for launched in self.phases]))

    def harnesses(self, stack: Stack) -> InstallResult:
        # The target runtime is published by now, and only its installed package
        # declares harness registration beside the operation family (``TargetProgram``).
        return stack.probe(HarnessesProbe(self.root, self.command.declared_part()))
