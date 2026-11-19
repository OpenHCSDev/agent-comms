"""The supported declaration-driven repository test environment operation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.runtime_home import RuntimeHome
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize, PathText
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_environment import RepositoryEnvironment
from agent_comms.projects.test_environment import DeclaredTestEnvironment
from agent_comms.transport.tool_caller import ThreadCaller


@dataclass(frozen=True)
class SyncedTestEnvironmentResult(CliResult):
    declaration: dict[str, object]


@dataclass(frozen=True, kw_only=True)
class SyncTestEnvironmentCliCommand(CliCommand, declared_name=RepositoryEnvironment.sync_verb):
    help = "Sync a shared test prefix from committed requirements or a locked pyproject.toml"
    caller: Annotated[ThreadCaller, Normalize(FieldCodec.encode),
                      Argument(flags=("--from",), options={"type": ThreadCaller})]
    checkout: Annotated[Path, PathText, Argument(flags=("--checkout",))]
    requirements: Annotated[Path, PathText, Argument(flags=("--requirements",))]
    project_thread: Annotated[str, Argument(flags=("--project-thread",))]

    def apply(self, ctx: Comms) -> SyncedTestEnvironmentResult:
        ctx.registry.require(self.caller.own_thread())
        checkout = Checkout.checked(self.checkout)
        project = ctx.registry.require(self.project_thread).worktree
        if (checkout.common_dir != project.common_dir and
                Repo.from_origin(checkout.path).github not in
                ctx.registry.snapshot().team_project.repository_names()):
            raise ValueError(f"{checkout.path} is not a worktree of {self.project_thread}'s repository")
        ctx.registry.storage_rule.require_floor()
        with InstallCommands() as commands:
            record = DeclaredTestEnvironment.selected(checkout, self.requirements).sync(
                commands, RuntimeHome.uv_path(Path.home()), LaunchRuntime.running().interpreter)
        return SyncedTestEnvironmentResult(record)
