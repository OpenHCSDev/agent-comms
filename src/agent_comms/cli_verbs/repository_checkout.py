from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.checkout import CheckoutCliCommand
from agent_comms.cli_verbs.cli_commands import CheckoutGrant
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.repository_sources import LocalRepositorySource, RepositorySource
from agent_comms.projects.storage_rules import RepositoryCheckoutAllocation, StorageRule
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class RepositoryCheckoutCliCommand(CheckoutCliCommand,
                                   declared_name=RepositoryCheckoutAllocation.verb):
    help = "Create, repair, transfer or remove an owned problem checkout of a registered OWNER/REPO"
    repository: Annotated[RepositoryName, Argument(flags=("repository",))]

    def declared_destination(self, rule: StorageRule, thread: Thread) -> Path:
        return rule.repository_checkout(rule.roots_of(thread), self.problem, self.repository)

    def source(self, ctx: Comms, thread: Thread) -> RepositorySource:
        repositories = ctx.registry.snapshot().team_project.require().repos
        selected = [repository for repository in repositories
                    if repository.github == self.repository]
        if not selected:
            raise ValueError(f'{self.repository.to_wire()} is not a registered repository.')
        repository, = selected
        return LocalRepositorySource(repository)
