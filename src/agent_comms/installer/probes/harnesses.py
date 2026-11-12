"""The harness options and registration shared by the CLI and runtime probe."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import (
    Comms,
    client_wire,
)
from agent_comms.installer.harness_install import Harness
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.nominal.field_codec import AbsolutePathText, Argument, PathText
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import TeamProject
from agent_comms.registry.threads import ExternalThread
from agent_comms.team.config import GivenRepos, KeptRepos, RepoChoice


@dataclass(frozen=True)
class InstallResult(CliResult):
    config: TeamProject
    registered: dict[str, tuple[str, ...]]


@dataclass(frozen=True, kw_only=True)
class HarnessInstallation:
    project: Annotated[
        Path,
        AbsolutePathText,
        Argument(
            flags=("--project",), options={"help": "The worktree harness threads register in"}
        ),
    ] = dataclasses.field(default_factory=Path.cwd)
    repos: Annotated[
        RepoChoice,
        Argument(
            flags=("--repos",),
            options={"help": "Found the team with these repositories or replace them"},
        ),
    ] = KeptRepos()

    def declared_part(self) -> HarnessInstallation:
        """Project a CLI subclass onto this operation's declared fields."""
        return HarnessInstallation(
            **{
                option.name: getattr(self, option.name)
                for option in dataclasses.fields(HarnessInstallation)
            }
        )

    def apply(self, ctx: Comms) -> InstallResult:
        """The install's last step, on the bus it carried or created, run by that bus's
        runtime (HarnessesProbe)."""
        home = Path.home()
        snapshot = ctx.registry.snapshot()
        repos = self.repos if snapshot.team_project.repository_names() else GivenRepos((self.project,))
        project = snapshot.team_project.reconfigured(repos)
        if project != snapshot.team_project:
            ctx.registry.set_team_project(project)
        return InstallResult(
            project,
            {
                type(harness).declared_name: self.declare_bus_threads(ctx, harness)
                for harness in Harness.present(home)
            },
        )

    def declare_bus_threads(self, ctx: Comms, harness: Harness) -> tuple[str, ...]:
        """Declare the bus threads this harness needs and the registry does not hold yet."""
        names = ctx.registry.snapshot()
        registered = tuple(name for name in harness.bus_threads if not names.taken(name))
        for name in registered:
            ctx.registry.declare(
                ExternalThread(name=name, tags=frozenset(), worktree=Checkout(self.project))
            )
        return registered


@dataclass(frozen=True)
class HarnessesProbe(TargetProbe, RuntimeProbe[InstallResult]):
    root: Annotated[Path, PathText]
    install: HarnessInstallation

    def run(self) -> InstallResult:
        with client_wire(self.root) as comms:
            return self.install.apply(comms)
