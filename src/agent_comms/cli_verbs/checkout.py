from __future__ import annotations

from agent_comms.cli_commands import CheckoutGrant
from agent_comms.cli_commands import OrderCheckoutGrant
from agent_comms.cli_support.prepared_checkout import PreparedCheckoutCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.storage_rules import OrderCheckoutAllocation
from agent_comms.storage_rules import StorageRule
from agent_comms.threads import Thread
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class CheckoutCliCommand(PreparedCheckoutCliCommand,
                         declared_name=OrderCheckoutAllocation.verb):
    help = "Create, repair or remove an owned problem checkout with its pinned submodules"
    problem: Annotated[str, Argument(flags=("problem",))]

    def grant(self, ctx: Comms, thread: Thread) -> CheckoutGrant:
        return OrderCheckoutGrant(thread, ctx.goals.todos.get(self.problem))

    def destination(self, rule: StorageRule, thread: Thread) -> Path:
        return rule.order_checkout(rule.roots_of(thread), self.problem)
