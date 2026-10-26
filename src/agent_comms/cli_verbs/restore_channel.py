from __future__ import annotations

from agent_comms.cli_command import CliCommand

from agent_comms.cli_verbs.archive_channel import ArchiveChannelCliCommand
from agent_comms.field_codec import Argument
from agent_comms.field_codec import TargetBound
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class RestoreChannelCliCommand(ArchiveChannelCliCommand, declared_name="restore-channel"):
    help = "Restore archived channel"
    archived: Annotated[
        bool, TargetBound(), Argument(flags=("--archived",))
    ] = False

    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        return cls
