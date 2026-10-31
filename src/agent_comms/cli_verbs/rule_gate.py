"""Configure the existing session deal without replacing its evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, PathText
from agent_comms.rule_deal import DealMode, SessionRuleDeal


@dataclass(frozen=True)
class RuleGateConfiguredResult(CliResult):
    stem: Annotated[Path, PathText]
    mode: type[DealMode]


@dataclass(frozen=True, kw_only=True)
class RuleGateCliCommand(CliCommand, declared_name="rule-gate"):
    help = "Configure a session's rule gate: full, startup (no automatic redeals), or off"
    stem: Annotated[Path, PathText, Argument(flags=("stem",))]
    mode: Annotated[type[DealMode], Argument(flags=("mode",))]

    def apply(self, ctx: Comms) -> CliResult:
        deal = SessionRuleDeal(self.stem.resolve())
        deal.configure(self.mode)
        return RuleGateConfiguredResult(deal.stem, deal.current().mode)
