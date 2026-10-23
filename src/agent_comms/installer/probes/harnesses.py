"""The harness options and registration shared by the CLI and runtime probe."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_result import CliResult
from agent_comms.commander.config import CommanderChoice, KeptCommander, KeptRepos, RepoChoice
from agent_comms.comms import Comms
from agent_comms.field_codec import AbsolutePathText, Argument, PathText
from agent_comms.harness_install import Harness
from agent_comms.installer.interpreter_operation import RuntimeProbe
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.registry_document import CommanderProject
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread


@dataclass(frozen=True)
class InstallResult(CliResult):
    config: CommanderProject
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
            flags=("--repos",), options={"help": "Replace the commander's recorded repositories"}
        ),
    ] = KeptRepos()
    commander: Annotated[
        CommanderChoice,
        Argument(
            flags=("--commander",), options={"help": "Record this thread as the bus's commander"}
        ),
    ] = KeptCommander()

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
        project = self.commander.configured(snapshot.commander_project, self.repos, snapshot)
        ctx.registry.set_commander_project(project)
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
                Thread(
                    name=name,
                    tags=frozenset(),
                    worktree=str(self.project),
                    execution=ExternalThreadExecution,
                )
            )
        return registered


@dataclass(frozen=True)
class HarnessesProbe(RuntimeProbe[InstallResult]):
    root: Annotated[Path, PathText]
    install: HarnessInstallation

    def run(self) -> InstallResult:
        from agent_comms.comms import client_wire

        with client_wire(self.root) as comms:
            return self.install.apply(comms)


@dataclass(frozen=True)
class RetireHarnessConfigurationProbe(RuntimeProbe[tuple[Annotated[Path, PathText], ...]]):
    root: Annotated[Path, PathText]

    def run(self) -> tuple[Annotated[Path, PathText], ...]:
        barrier = MaintenanceBarrier(RegistryStore.path_in(self.root))
        barrier.read().require_held_by(
            MaintenanceHolder.current().nonce, 'Retiring superseded harness configuration')
        return tuple(path for harness in Harness.present(Path.home())
                     for path in harness.retire_configuration())
