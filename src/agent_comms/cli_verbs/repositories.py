from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult


@dataclass(frozen=True)
class RegisteredRepository:
    """One repository the team works on, and the checkout it reads it from."""

    github: str
    checkout: Path


@dataclass(frozen=True)
class RepositoriesResult(CliResult):
    repositories: tuple[RegisteredRepository, ...]


@dataclass(frozen=True, kw_only=True)
class RepositoriesCliCommand(CliCommand, declared_name="repositories"):
    """The team project's own checkouts: reading one needs no allocation, writing one does."""

    help = "List the registered repositories and the checkout the team reads them from"

    def apply(self, ctx: CliBus) -> RepositoriesResult:
        project = ctx.registry.snapshot().team_project.require()
        return RepositoriesResult(tuple(
            RegisteredRepository(repo.github.to_wire(), repo.checkout) for repo in project.repos
        ))
