from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_commands import CliCommand, TargetActionCliCommand, TargetBatchResult
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.installer.interpreter_operation import RuntimeProbe
from agent_comms.installer.owner_starts import OwnerStart
from agent_comms.installer.probes.commands import CliCommandProbe
from agent_comms.owner_restart import OwnerRestartRequest


@dataclass(frozen=True)
class OwnersProbe(RuntimeProbe[tuple[OwnerStart, ...]]):
    """The selected runtime owns decoding its command results."""

    root: Annotated[Path, PathText]
    command: type[CliCommand]
    names: tuple[str, ...]

    def run(self) -> tuple[OwnerStart, ...]:
        invocation = TargetActionCliCommand.invocation(
            "--operation", self.command.declared_name, "--targets", *self.names)
        completed = CliCommandProbe(("--root", str(self.root), *invocation)).run()
        result = FieldCodec.decode(TargetBatchResult, FieldCodec.parse(completed.stdout))
        return tuple(outcome.owner_start() for outcome in result.outcomes)


@dataclass(frozen=True)
class RunningOwnersProbe(RuntimeProbe[list[str]]):
    root: Annotated[Path, PathText]

    def run(self) -> list[str]:
        snapshot = wire(self.root).registry.snapshot()
        return [thread.name for thread in OwnerRestartRequest().scope.threads(snapshot)]
