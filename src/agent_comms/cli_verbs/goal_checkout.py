"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_commands import CheckoutGrant
from agent_comms.cli_commands import GoalCheckoutGrant
from agent_comms.cli_verbs.prepared_checkout import PreparedCheckoutCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import PathText
from agent_comms.repo import Repo
from agent_comms.storage_rules import StorageRule
from agent_comms.threads import Thread
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class GoalCheckoutCliCommand(PreparedCheckoutCliCommand, declared_name="goal-checkout"):
    help = "Create, repair or remove an isolated repository checkout for your own goal"
    repository: Annotated[Path, PathText, Argument(flags=("repository",))]

    @cached_property
    def selected_repository(self) -> Repo:
        return Repo.of(self.repository.resolve())

    def grant(self, ctx: Comms, thread: Thread) -> CheckoutGrant:
        return GoalCheckoutGrant(thread, thread.goal.require(), self.selected_repository)

    def destination(self, rule: StorageRule, thread: Thread) -> Path:
        return rule.repository_checkout(
            rule.roots_of(thread), thread.goal.require().id, self.selected_repository.github,
        )
