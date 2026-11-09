from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.execution.imported_messages import ImportLimits
from agent_comms.nominal.field_codec import Argument


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
