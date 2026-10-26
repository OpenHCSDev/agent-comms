"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.field_codec import Argument
from agent_comms.importing import ImportLimits
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ConversationLimitsCliCommand(CliCommand):
    """How much of a source conversation an imported Pi session keeps."""

    max_messages: Annotated[int, Argument(flags=("--max-messages",))] = (
        200
    )
    max_characters: Annotated[int, Argument(flags=("--max-characters",))] = (
        120000
    )
    max_message_characters: Annotated[
        int, Argument(flags=("--max-message-characters",))
    ] = 12000

    @property
    def limits(self) -> ImportLimits:
        return ImportLimits(self.max_messages, self.max_characters, self.max_message_characters)
