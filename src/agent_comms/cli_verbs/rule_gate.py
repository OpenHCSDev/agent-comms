"""Configure the existing session deal without replacing its evidence."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import TargetEditResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, PathText, RecordMember
from agent_comms.team.rule_deal import RuleDeal, RuleGateSettings, SessionRuleDeal


@dataclass(frozen=True)
class RuleGateConfiguredResult(RecordMember, TargetEditResult):
    stem: Annotated[Path, PathText]
    settings: RuleGateSettings


@dataclass(frozen=True, kw_only=True)
class RuleGateCommand(RuleGateSettings, CliCommand, declared_name=None):
    help = "Configure this session's rule reads and refreshes"

    @abstractmethod
    def deal(self, ctx: Comms) -> RuleDeal: ...

    def apply(self, ctx: Comms) -> RuleGateConfiguredResult:
        deal = self.deal(ctx)
        deal.configure(RuleGateSettings.capture(self))
        return RuleGateConfiguredResult(deal.stem, RuleGateSettings.capture(deal.current()))


@dataclass(frozen=True, kw_only=True)
class RuleGateCliCommand(RuleGateCommand, declared_name="rule-gate"):
    stem: Annotated[Path, PathText, Argument(flags=("stem",))]

    def deal(self, ctx: Comms) -> RuleDeal:
        return SessionRuleDeal(self.stem.resolve())
