from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import TargetActionCommand
from agent_comms.cli_verbs.cli_commands import OwnersRestartedResult, _shell_words
from agent_comms.cli_verbs.target_fields import TargetField
from agent_comms.comms import Comms
from agent_comms.execution.native_arguments import (
    ConfiguredBinary,
    InheritedArguments,
    LaunchArguments,
    LaunchBinary,
)
from agent_comms.execution.restart_scope import AllOwners, RestartTarget
from agent_comms.nominal.field_codec import (
    Argument,
    NamedArgumentGroup,
    Normalize,
    TargetBound,
    WireName,
)


@dataclass(frozen=True, kw_only=True)
class RestartCliCommand(TargetActionCommand[OwnersRestartedResult]):
    help = "Restart process"
    name: Annotated[
        RestartTarget,
        TargetBound(),
        Argument(
            flags=("--name",),
            group=NamedArgumentGroup("scope"),
            options={"help": "One running thread (aliases supported)"},
        ),
    ] = AllOwners()
    all_: Annotated[
        bool,
        WireName("all"),
        TargetBound(),
        Argument(
            flags=("--all",),
            group=NamedArgumentGroup("scope"),
            options={"help": "All running owners; excludes stopped threads"},
        ),
    ] = False
    agent_bin: Annotated[LaunchBinary, Argument(flags=("--agent-bin",))] = ConfiguredBinary()
    agent_args: Annotated[
        LaunchArguments,
        Normalize(_shell_words),
        Argument(flags=("--agent-args",)),
    ] = InheritedArguments()

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        # A selected thread restart retains its existing launch declaration.
        # Explicit CLI launch overrides are independent operator arguments.
        return ()

    def apply(self, ctx: Comms) -> OwnersRestartedResult:
        results = ctx.owners.restart_owners(self.name, agent_bin=self.agent_bin, agent_args=self.agent_args)
        return OwnersRestartedResult(results)
