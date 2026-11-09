from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_support.constraint_pin import OriginalConstraintPin
from agent_comms.cli_verbs.cli_commands import ConstraintPinnedResult
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.execution.input_key import InputKey, InputKeyText
from agent_comms.input_disposition import InputDispositions
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True, kw_only=True)
class PinInputConstraintCliCommand(OriginalConstraintPin, declared_name="pin-input-constraint"):
    help = "Pin an original human input for its recipient"
    source: Annotated[
        InputKey, InputKeyText,
        Argument(flags=("--source",), options={"help": "Original durable input key"}),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        source = InputDispositions(ctx.root).lookup(self.source).context_provenance().require_human_input()
        pin = ctx.messaging.pin_input_constraint(self.thread, source, worktree=self.worktree)
        return ConstraintPinnedResult(pin.reference, source)
