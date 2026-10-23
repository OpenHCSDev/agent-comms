"""One install, run from the target revision's own source in a session of its own.

The installed CLI only fetches, resolves the target and checks its tree out; the install
itself runs that tree's ``src`` (``python -m agent_comms.installer``), so its carries,
rehearsal and harness setup are the target's, never the outgoing runtime's.
"""
from __future__ import annotations

from agent_comms.active_route import resolve_comms_route

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
from pathlib import Path
from traceback import extract_tb
from typing import Annotated, TextIO

import agent_comms.installer
from agent_comms.cli_bridges.shared import SessionBridgeResumes
from agent_comms.cli_commands import InstallCliCommand, InstallResult
from agent_comms.comms import client_wire
from agent_comms.field_codec import FieldCodec, Id128Text, PathText
from agent_comms.installer.backup import StoreBackup, UpgradeRefused
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.reporting import (
    FinishedInstall, InstallOutcome, RootWriter, UnfinishedInstall, UnpostedWriter,
)
from agent_comms.installer.upgrade import (
    RehearsedUpgrade,
    TargetTree,
    Upgrade,
    upgrade,
)
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.storage_rules import StorageRule


@dataclass
class InstallReport(MroDispatch):
    """The operation owns one terminal report, after its resources have closed."""

    writer: RootWriter = field(default_factory=UnpostedWriter)
    outcome: InstallOutcome = field(default_factory=UnfinishedInstall, init=False)
    bridges: SessionBridgeResumes = field(default_factory=SessionBridgeResumes, init=False)

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
        frame = extract_tb(error.__traceback__)[-1]
        return (f"Install failed: {type(error).__name__}: {error}",
                f"{frame.filename}:{frame.lineno}")

    @contextmanager
    def running(self, tree: TargetTree) -> Iterator[None]:
        """The writer can run the target's CLI from ``tree``, so the tree closes after the
        report is posted."""
        with tree:
            try:
                yield
                lines = self.outcome.lines()
            except BaseException as error:
                failure = (*next(self.handlers_for(error))(error), *self.bridges.lines())
                try:
                    self.writer.post(failure)
                except BaseException as publication_error:
                    error.add_note(f"Install failure report also failed: {publication_error}")
                    print(f"install: failure report also failed: {publication_error}",
                          file=sys.stderr)
                raise
            else:
                self.writer.post(lines)


@dataclass(frozen=True)
class InstallLogs:
    """Each install's diagnostics beside the bus, kept as long as its data backups are."""

    root: Path

    @property
    def prefix(self) -> str:
        return f".{self.root.name}.installer-"

    def minted(self) -> Path:
        """A new install's own directory; older ones past the backups' count are retired."""
        logs = self.root.parent / f"{self.prefix}{Id128Text.mint()}"
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

    @classmethod
    def start(cls, command: InstallCliCommand, root: Path) -> InstallRun:
        checkout = command.checkout
        commands = InstallCommands()
        commands.run("git", "-C", checkout, "fetch", "-q", "origin")
        target = commands.run("git", "-C", checkout, "rev-parse", "--verify",
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
        logs = InstallLogs(self.root).minted()
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
                       command.mode, command.retire, command.spill_dir, self.scratch,
                       self.harnesses, InstallReport())

    def harnesses(self) -> InstallResult:
        with client_wire(resolve_comms_route(self.root)) as comms:
            return self.command.apply(comms)
