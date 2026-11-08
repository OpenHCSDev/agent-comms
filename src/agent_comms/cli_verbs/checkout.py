from __future__ import annotations

from agent_comms.cli_commands import CheckoutGrant
from agent_comms.cli_commands import OrderCheckoutGrant
from agent_comms.cli_support.prepared_checkout import PreparedCheckoutCliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from ..projects.repository_sources import (
    LocalRepositorySource,
    RepositorySource,
)
from ..projects.storage_rules import OrderCheckoutAllocation
from ..projects.storage_rules import StorageRule
from agent_comms.registry.threads import Thread
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class CheckoutCliCommand(PreparedCheckoutCliCommand,
                         declared_name=OrderCheckoutAllocation.verb):
    help = "Create, repair, transfer or remove an owned problem checkout with its pinned submodules"
    problem: Annotated[str, Argument(flags=("problem",))]

    def grant(self, ctx: Comms, thread: Thread) -> CheckoutGrant:
        return OrderCheckoutGrant(thread, ctx.goals.todos.get(self.problem))

    def source(self, ctx: Comms, thread: Thread) -> RepositorySource:
        return LocalRepositorySource(ctx.goals.todos.get(self.problem).repository)

    def declared_destination(self, rule: StorageRule, thread: Thread) -> Path:
        return rule.order_checkout(rule.roots_of(thread), self.problem)
