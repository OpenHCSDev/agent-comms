"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ConstraintPinnedResult
from agent_comms.cli_commands import WorktreeArgument
from agent_comms.cli_commands import _message_reference
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Normalize
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class PinConstraintCliCommand(WorktreeArgument, CliCommand, declared_name="pin-constraint"):
    help = "Pin a checked original human message for its recipient"
    thread: Annotated[str, Argument(flags=("thread",))]
    source: Annotated[
        MessageReference,
        Normalize(_message_reference),
        Argument(flags=("--source",), options={"help": "Original sequence:message_id"}),
    ]

    def original_source(self, ctx: Comms):
        return self.source

    def pin_original(self, ctx: Comms, source: MessageReference) -> Message:
        return ctx.messaging.pin_user_constraint(self.thread, source, worktree=self.worktree)

    def apply(self, ctx: Comms) -> CliResult:

        source = self.original_source(ctx)
        pin = self.pin_original(ctx, source)
        return ConstraintPinnedResult(pin.reference, source)
