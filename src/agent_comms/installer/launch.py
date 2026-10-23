"""One install, run from the target revision's own source in a session of its own.

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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, TextIO

import agent_comms.installer
from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import CommandTarget, CommsTag
from agent_comms.cli_commands import InstallCliCommand, InstallResult
from agent_comms.comms import client_wire
from agent_comms.field_codec import FieldCodec, Id128Text, PathText
from agent_comms.installer.backup import StoreBackup, UpgradeRefused
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.upgrade import (
    InstalledUpgrade,
    TargetTree,
    Upgrade,
    upgrade,
)
from agent_comms.messages import InfoKind, Message
from agent_comms.service_sender import ServiceSender
from agent_comms.storage_rules import StorageRule


@dataclass(frozen=True)
class InstallReport(ServiceSender):
    """Publish the install's own typed result, never an interpretation of its logs."""

    sender: ClassVar[str] = "installer"
    root: Path

    def post(self, lines: tuple[str, ...]) -> Message:
        with client_wire(self.root) as comms:
            self.register(comms.registry)
            return comms.bus.publisher.publish_ordinary(
                Message(sender=self.sender, target=CommsTag.channel(),
                        body="\n".join(lines),
                        type=InfoKind),
                destination=CommandTarget.named(CommsTag.channel()), origin=AgentOrigin())

    def starting(self, upgrade: Upgrade) -> Message:
        return self.post(("Install starting", *upgrade.lines()))

    def finished(self, upgrade: InstalledUpgrade) -> Message:
        return self.post((upgrade.finished, *upgrade.lines()))

    def failed(self, error: UpgradeRefused) -> Message:
        return self.post(error.lines())


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
        checkout = command.checkout.resolve()
        commands = InstallCommands()
        commands.run("git", "-C", checkout, "fetch", "-q", "origin")
        target = commands.run("git", "-C", checkout, "rev-parse", "--verify",
                     f"{command.to}^{{commit}}").strip()
        parent = command.installer.scratch(StorageRule.load(root))
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(dir=parent, prefix="install-"))
        TargetTree(checkout, scratch, target).add()
        # Paths resolve here: the install child runs in the target tree, not the caller's cwd.
        return cls(replace(command, checkout=checkout, project=command.project.resolve(),
                           repos=command.repos.resolved()),
                   root, scratch, target)

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
                       command.rehearse, command.retire, command.spill_dir, self.scratch,
                       self.harnesses, InstallReport(self.root))

    def harnesses(self) -> InstallResult:
        with client_wire(self.root) as comms:
            return self.command.apply(comms)
