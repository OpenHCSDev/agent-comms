from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import OwnersRestartedResult
from agent_comms.cli_commands import _shell_words
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import NamedArgumentGroup
from agent_comms.nominal.field_codec import Normalize
from agent_comms.nominal.field_codec import TargetBound
from agent_comms.nominal.field_codec import WireName
from agent_comms.native_arguments import ConfiguredBinary
from agent_comms.native_arguments import InheritedArguments
from agent_comms.native_arguments import LaunchArguments
from agent_comms.native_arguments import LaunchBinary
from agent_comms.restart_scope import AllOwners
from agent_comms.restart_scope import RestartTarget
from agent_comms.target_fields import TargetField
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class RestartCliCommand(CliCommand):
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

    def apply(self, ctx: Comms) -> CliResult:
        results = ctx.owners.restart_owners(self.name, agent_bin=self.agent_bin, agent_args=self.agent_args)
        return OwnersRestartedResult(results)
