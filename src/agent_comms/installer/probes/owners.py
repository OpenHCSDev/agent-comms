from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from typing import Annotated, TextIO

from agent_comms.cli_command import TargetBatchResult
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.installer.interpreter_operation import RuntimeProbe
from agent_comms.installer.probes.commands import CliCommandProbe
from agent_comms.installer.owner_starts import OwnerStart
from agent_comms.owner_restart import OwnerRestartRequest


class OwnerActionsProbe(CliCommandProbe):
    """The selected runtime decodes its own batch before emitting owner reports."""

    def execute(self, arguments: Sequence[str], output: TextIO) -> int:
        command_output = StringIO()
        super().execute(arguments, command_output)
        batch = FieldCodec.decode(TargetBatchResult, FieldCodec.parse(command_output.getvalue()))
        owners = tuple(outcome.owner_start() for outcome in batch.outcomes)
        print(FieldCodec.compact(FieldCodec.encode(owners, tuple[OwnerStart, ...])), file=output)
        return 0


@dataclass(frozen=True)
class RunningOwnersProbe(RuntimeProbe[list[str]]):
    root: Annotated[Path, PathText]

    def run(self) -> list[str]:
        snapshot = wire(self.root).registry.snapshot()
        return [thread.name for thread in OwnerRestartRequest().scope.threads(snapshot)]
