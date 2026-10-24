"""One install, run from the target revision's own source in a systemd unit of its own.

The installed CLI only fetches, resolves the target and checks its tree out; the install
itself runs that tree's ``src`` (``python -m agent_comms.installer``), so its carries,
rehearsal and harness setup are the target's, never the outgoing runtime's.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from traceback import extract_tb
from typing import Annotated, ClassVar, TextIO

import agent_comms.installer
from agent_comms.cli_bridges.shared import SessionBridgeResumes
from agent_comms.cli_commands import InstallCliCommand
from agent_comms.commander import standing
from agent_comms.field_codec import FieldCodec, Id128, PathText
from agent_comms.host_services import SystemdUnit
from agent_comms.installer.backup import StoreBackup
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
    RehearsedUpgrade,
    Stack,
    TargetTree,
    Upgrade,
    upgrade,
)
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageRule


@dataclass
class InstallReport(MroDispatch):
    """The operation owns one terminal report, after its resources have closed."""

    writer: RootWriter = field(default_factory=UnpostedWriter)
    outcome: InstallOutcome = field(default_factory=UnfinishedInstall, init=False)
    bridges: SessionBridgeResumes = field(default_factory=SessionBridgeResumes, init=False)
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
        package = Path(agent_comms.__file__).resolve().parent
        frame = next(
            frame for frame in reversed(extract_tb(error.__traceback__))
            if Path(frame.filename).resolve().is_relative_to(package)
        )
        module = ".".join(
            Path(frame.filename).resolve().relative_to(package.parent).with_suffix("").parts
        )
        location = self.source.module_file(module)
        return (f"Install failed: {type(error).__name__}: {error}",
                f"{location}:{frame.lineno}")

    @contextmanager
    def running(self, tree: TargetTree, clock: InstallClock) -> Iterator[None]:
        """Every writer runs installed artifacts from outside ``tree``, so the report follows
        the tree's cleanup and a cleanup failure is the install's failure."""
        self.source = Repo(checkout=tree.path, github=CorePackage.repository)
        try:
            tree.__enter__()
            try:
                yield
            finally:
                with TreeClosePhase().running(clock):
                    tree.__exit__(*sys.exc_info())
            lines = self.outcome.lines(tuple(self.inherited_phases))
        except BaseException as error:
            self.outcome = FailedInstall(self.outcome, next(self.handlers_for(error))(error))
            try:
                with FailureReportPublicationPhase().running(clock):
                    self.writer.post((*self.outcome.lines(), *self.bridges.lines()))
            except BaseException as publication_error:
                error.add_note(f"Install failure report also failed: {publication_error}")
                print(f"install: failure report also failed: {publication_error}", file=sys.stderr)
            raise
        else:
            with FinishedReportPublicationPhase().running(clock):
                self.writer.post(lines)


@dataclass(frozen=True)
class InstallCause:
    """The repository merge that caused an automatic install, independent of its target."""

    repo: RepositoryName
    main: str


@dataclass(frozen=True)
class InstallUnit(SystemdUnit):
    """One install as a transient user unit, named by its diagnostics directory.

    Systemd owns its lifetime exactly as it owns the connector's, so an install the heartbeat
    starts from inside the connector's unit survives stopping that unit."""

    logs: Path

    @property
    def unit(self) -> str:
        return f"agent-comms-install-{self.logs.name.rpartition('-')[2]}.service"

    @property
    def result(self) -> Path:
        return self.logs / "result.json"

    @property
    def errors(self) -> Path:
        return self.logs / "stderr.log"

    def start(self, tree: Path, command: tuple[str, ...], env: dict[str, str],
              *options: str) -> None:
        """Start the unit; ``--wait`` follows it to its end and raises on its failure."""
        subprocess.run(
            ("systemd-run", "--user", "--quiet", "--collect", *options, f"--unit={self.unit}",
             f"--working-directory={tree}", f"--property=StandardOutput=file:{self.result}",
             f"--property=StandardError=file:{self.errors}",
             *(f"--setenv={name}={value}" for name, value in env.items()), *command),
            check=True,
        )

    def due(self) -> type[standing.InstallDue]:
        """A run whose unit is gone without printing a result died; any other was launched."""
        gone = self.shown(self.unit, "ActiveState") in ("inactive", "failed")
        return (standing.DiedInstallDue if gone and not self.result.read_text()
                else standing.LaunchedInstallDue)

    def last_error(self) -> str:
        return next((line for line in reversed(self.errors.read_text().splitlines())
                     if line.strip()), "no output")


@dataclass(frozen=True)
class InstallLogs:
    """Each install's diagnostics beside the bus, kept as long as its data backups are."""

    root: Path
    cause_file: ClassVar[str] = "cause.json"

    def launched(self, repo: RepositoryName, main: str) -> tuple[InstallUnit, ...]:
        """The runs launched for this merge, decoded once from their launch records."""
        cause = InstallCause(repo, main)
        return tuple(
            InstallUnit(path)
            for path in self.root.parent.glob(f"{glob.escape(self.prefix)}*")
            if path.is_dir()
            for marker in (path / self.cause_file,)
            if marker.is_file()
            and FieldCodec.decode(InstallCause, FieldCodec.parse(marker.read_text())) == cause
        )

    def member(self, repo: RepositoryName, main: str) -> type[standing.InstallDue]:
        """The merge's launched run selects its standing-rule member; none is pending."""
        return next((run.due() for run in self.launched(repo, main)),
                    standing.PendingInstallDue)

    def record(self, logs: Path, repo: RepositoryName, main: str) -> None:
        (logs / self.cause_file).write_text(json.dumps(FieldCodec.encode(InstallCause(repo, main))))

    @property
    def prefix(self) -> str:
        return f".{self.root.name}.installer-"

    def minted(self) -> Path:
        """A new install's own directory; older ones past the backups' count are retired."""
        logs = self.root.parent / f"{self.prefix}{Id128.mint()}"
        logs.mkdir(mode=0o700)
        self.retire()
        return logs

    def retire(self) -> tuple[Path, ...]:
        """The names carry no time, so age is each directory's own modification time."""
        logs = sorted((path for path in self.root.parent.glob(f"{glob.escape(self.prefix)}*")
                       if path.is_dir()), key=lambda path: path.stat().st_mtime)
        retired = tuple(logs[:-StoreBackup.kept])
        for path in retired:
            shutil.rmtree(path)
        return retired


@dataclass(frozen=True)
class InstallRun:
    command: InstallCliCommand
    root: Annotated[Path, PathText]
    scratch: Annotated[Path, PathText]
    target: str
    phases: list[LaunchedPhaseTiming] = field(default_factory=list)

    @classmethod
    def start(cls, command: InstallCliCommand, root: Path) -> InstallRun:
        checkout = command.checkout
        with InstallCommands() as commands, LauncherPhase().running(commands.clock):
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

    @cached_property
    def logs(self) -> Path:
        return InstallLogs(self.root).minted()

    @cached_property
    def unit(self) -> InstallUnit:
        return InstallUnit(self.logs)

    def spawn(self, *options: str) -> None:
        """The install runs in its own unit and reads its request from a file beside the bus,
        so stopping the caller's unit, the connector's included, cannot cut it short."""
        tree = TargetTree(self.command.checkout, self.scratch, self.target)
        request = self.logs / "request.json"
        request.write_text(json.dumps(FieldCodec.encode(self)))
        print(f"install: {self.unit.unit} runs {self.target[:12]}; "
              f"diagnostics {self.unit.errors}", file=sys.stderr, flush=True)
        self.unit.start(
            tree.path, (sys.executable, "-m", agent_comms.installer.__name__, str(request)),
            {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, tree.source_paths()))},
            *options,
        )

    def launch(self, output: TextIO) -> int:
        try:
            self.spawn("--wait")
        except subprocess.CalledProcessError as failed:
            print(f"install failed ({failed.returncode}): {self.unit.errors.read_text().strip()}",
                  file=sys.stderr)
            return failed.returncode
        output.write(self.unit.result.read_text())
        return 0

    def run(self) -> Upgrade:
        command = self.command
        return upgrade(command.checkout, self.root, self.target, command.installer,
                       command.mode, command.retire, command.spill_dir, self.scratch,
                       self.harnesses, InstallReport(
                           inherited_phases=[launched.timing() for launched in self.phases]))

    def harnesses(self, stack: Stack) -> InstallResult:
        # The target runtime is published by now, and only its installed package
        # declares harness registration beside the operation family (``TargetProgram``).
        return stack.probe(HarnessesProbe(self.root, self.command.declared_part()))
