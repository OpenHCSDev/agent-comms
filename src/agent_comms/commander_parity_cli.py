"""Run comparisons through the actual application, with isolated script configuration."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from .check_root import CheckRoot
from .cli_commands import CliCommand, option
from .commander.config import CommanderConfig
from .comms import Comms
from .field_codec import FieldCodec, PathText
from .launch_runtime import RestartEnvironment, ThreadLaunch
from .parity_checks import CommanderComparison, CommanderParityCheck, CommanderParityReport


@dataclass(frozen=True)
class ParityMatrix:
    """Decode the existing Markdown matrix once; never identify a row by its title."""

    rows: tuple[int, ...]

    @classmethod
    def read(cls, path: Path) -> ParityMatrix:
        text = path.read_text()
        try:
            section = text.split("## Parity matrix\n", 1)[1].split("\n## ", 1)[0]
        except IndexError as error:
            raise ValueError(f"No parity matrix in {path}") from error
        rows = []
        for line in section.splitlines():
            if not line.startswith("|"):
                continue
            first = line.split("|")[1].strip()
            if first == "#" or first and set(first) <= {"-", ":"}:
                continue
            rows.append(int(first))
        if not rows or any(number <= 0 for number in rows) or len(set(rows)) != len(rows):
            raise ValueError("A parity matrix needs unique positive row numbers")
        return cls(tuple(rows))

    def compare(self, context: CommanderComparison) -> CommanderParityReport:
        checks = CommanderParityCheck.members_with(CommanderParityCheck)
        numbers = tuple(check.number for check in checks)
        if len(set(numbers)) != len(numbers) or set(numbers) - set(self.rows):
            raise ValueError("Parity declarations duplicate a row or name a row outside the matrix")
        comparisons = tuple(
            check.compare(context) for check in sorted(checks, key=lambda check: check.number))
        complete = {row.check.number for row in comparisons
                    if row.coverage.complete and row.observations}
        return CommanderParityReport(
            comparisons, tuple(number for number in self.rows if number not in complete))


@dataclass(frozen=True, kw_only=True)
class CommanderParityCliCommand(CliCommand, declared_name="commander-parity"):
    help = "Compare native/current commander behaviour in an isolated saved-history root"
    plan: Annotated[Path, PathText] = option(help="Markdown plan containing the numbered parity matrix")
    into: Annotated[Path, PathText] = option(help="New directory under your agent scratch directory")

    def apply(self, ctx: Comms) -> CommanderParityReport:
        config = CommanderConfig.installed()
        if config.root.resolve() != ctx.root.resolve():
            raise ValueError("The current commander configuration belongs to another root")
        with CheckRoot(ctx, (config.commander,), self.into).open() as copied:
            home = copied.root / "home"
            config.model_copy(update={
                "root": copied.root, "state": copied.root / "state",
                "board": copied.root / "board.md", "narrative": copied.root / "narrative.md",
                "scratch": copied.root / "scratch", "disk": copied.root,
            }).write(home)
            environment = ThreadLaunch.strip(dict(os.environ))
            RestartEnvironment.strip(environment, "root")
            environment.update(HOME=str(home), PYTHONPATH=os.pathsep.join(sys.path))
            # A separate interpreter prevents the current scripts' import-time
            # globals from ever binding this check to the original live root.
            result = subprocess.run(
                (sys.executable, "-m", "agent_comms.cli", "--root", str(copied.root),
                 "commander-parity-observe", "--plan", str(self.plan.resolve())),
                env=environment, capture_output=True, text=True, timeout=60,
            )
            try:
                report = FieldCodec.decode(CommanderParityReport, FieldCodec.parse(result.stdout))
            except ValueError as error:
                raise ValueError(
                    f"Parity worker produced no report (exit {result.returncode}): "
                    f"{result.stdout}\n{result.stderr}"
                ) from error
            if result.returncode != report.exit_code():
                raise ValueError(f"Parity worker exit disagrees with its report: {result.stderr}")
            return report


@dataclass(frozen=True, kw_only=True)
class ObserveCommanderParityCliCommand(CliCommand, declared_name="commander-parity-observe"):
    help = "Observe declared comparisons; commander-parity supplies an isolated root"
    plan: Annotated[Path, PathText] = option(help="Markdown plan containing the numbered parity matrix")

    def apply(self, ctx: Comms) -> CommanderParityReport:
        # This is the real current commander's function, not a reimplementation
        # of its refusals or a mocked transport. Its globals are isolated above.
        from .commander import order

        if order.COMMS.root.resolve() != ctx.root.resolve():
            raise ValueError("The current scripts opened another root; no comparison ran")
        return ParityMatrix.read(self.plan).compare(CommanderComparison(order.refuse))
