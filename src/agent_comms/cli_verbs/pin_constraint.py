from __future__ import annotations

from agent_comms.cli_support.constraint_pin import OriginalConstraintPin
from agent_comms.cli_commands import ConstraintPinnedResult
from agent_comms.cli_commands import _message_reference
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import Normalize
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class PinConstraintCliCommand(OriginalConstraintPin, declared_name="pin-constraint"):
    help = "Pin a checked original human message for its recipient"
    source: Annotated[
        MessageReference,
        Normalize(_message_reference),
        Argument(flags=("--source",), options={"help": "Original sequence:message_id"}),
    ]

    def pin_original(self, ctx: Comms, source: MessageReference) -> Message:
        return ctx.messaging.pin_user_constraint(self.thread, source, worktree=self.worktree)

    def apply(self, ctx: Comms) -> CliResult:
        return ConstraintPinnedResult(self.pin_original(ctx, self.source).reference, self.source)
