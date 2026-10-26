"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_verbs.pin_constraint import PinConstraintCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_key import InputKey
from agent_comms.input_key import InputKeyText
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class PinInputConstraintCliCommand(PinConstraintCliCommand, declared_name="pin-input-constraint"):
    help = "Pin an original human input for its recipient"
    source: Annotated[
        InputKey, InputKeyText,
        Argument(flags=("--source",), options={"help": "Original durable input key"}),
    ]

    def original_source(self, ctx: Comms):
        return InputDispositions(ctx.root).lookup(self.source).context_provenance().require_human_input()

    def pin_original(self, ctx: Comms, source: MessageReference) -> Message:
        return ctx.messaging.pin_input_constraint(self.thread, source, worktree=self.worktree)
