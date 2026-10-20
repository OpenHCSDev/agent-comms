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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, TextIO

from agent_comms.cli_commands import InstallCliCommand
from agent_comms.comms import client_wire
from agent_comms.field_codec import FieldCodec, Id128Text, PathText
from agent_comms.installer.upgrade import Revisions, run, upgrade
from agent_comms.storage_rules import StorageRule


@dataclass(frozen=True)
class InstallRun:
    command: InstallCliCommand
    root: Annotated[Path, PathText]
    scratch: Annotated[Path, PathText]
    target: str

    @classmethod
    def start(cls, command: InstallCliCommand, root: Path) -> InstallRun:
        checkout = command.checkout.resolve()
        run("git", "-C", checkout, "fetch", "-q", "origin")
        target = run("git", "-C", checkout, "rev-parse", "--verify", f"{command.to}^{{commit}}").strip()
        parent = StorageRule.load(root).initial_roots(command.installer).scratch
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(dir=parent, prefix="install-"))
        Revisions(checkout, scratch).add(target)
        return cls(replace(command, checkout=checkout), root, scratch, target)

    def launch(self, output: TextIO) -> int:
        """The install outlives its caller: a new session, its output in files beside the bus.
        Retiring the caller's process group must not cut an install short."""
        tree = Revisions(self.command.checkout, self.scratch).path(self.target)
        logs = self.root.parent / f".{self.root.name}.installer-{Id128Text.mint()}"
        logs.mkdir(mode=0o700)
        result, errors = logs / "result.json", logs / "stderr.log"
        with result.open("xb") as stdout, errors.open("xb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-m", "agent_comms.installer"], cwd=tree,
                env={**os.environ, "PYTHONPATH": str(tree / "src")},
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

    def run(self) -> dict:
        """Upgrade, then register the harnesses on the bus the target now reads."""
        command = self.command
        report = upgrade(command.checkout, self.root, self.target, command.installer,
                         command.rehearse, command.retire, command.spill_dir, self.scratch)
        if command.rehearse:
            return report
        with client_wire(self.root) as comms:
            return {**report, "install": FieldCodec.encode(command.apply(comms))}
