from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_support.prepared_checkout import PreparedCheckoutCliCommand
from agent_comms.cli_verbs.cli_commands import CheckoutGrant, GoalCheckoutGrant
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.projects.repository_sources import RepositorySource
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class GoalCheckoutCliCommand(PreparedCheckoutCliCommand, declared_name="goal-checkout"):
    help = "Create, repair or remove an isolated repository checkout for your own goal"
    repository: Annotated[RepositorySource, Argument(flags=("repository",))]

    def grant(self, ctx: Comms, thread: Thread) -> CheckoutGrant:
        return GoalCheckoutGrant(thread, thread.goal.require())

    def source(self, ctx: Comms, thread: Thread) -> RepositorySource:
        return self.repository

    def declared_destination(self, rule: StorageRule, thread: Thread) -> Path:
        return rule.repository_checkout(rule.roots_of(thread), thread.goal.require().id,
                                        self.repository.github)
