from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_verbs.cli_command import TargetActionCommand
from agent_comms.cli_verbs.rule_gate import RuleGateCommand, RuleGateConfiguredResult
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import SurroundingChannel
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread
from agent_comms.team.pi_rule_gate import PiThreadGate
from agent_comms.team.rule_deal import RuleDeal, RuleGateSettings


@dataclass(frozen=True, kw_only=True)
class ThreadRuleGateCliCommand(NamedCliCommand, RuleGateCommand,
                              TargetActionCommand[RuleGateConfiguredResult],
                              declared_name="configure-rule-gate"):
    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus,
                        channel: SurroundingChannel, *, catalog: CatalogAccess) -> tuple[Self, ...]:
        deal = PiThreadGate.of(comms, thread)
        if not deal.record.exists():
            return ()
        return (cls(name=thread.name, **{
            plan.name: plan.current(deal.current())
            for plan in FieldCodec.field_plans(RuleGateSettings)
        }),)

    def deal(self, ctx: Comms) -> RuleDeal:
        return PiThreadGate.of(ctx, ctx.registry.require(self.name))
