from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.archive_channel import ArchiveChannelCliCommand
from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.nominal.field_codec import Argument, TargetBound


@dataclass(frozen=True, kw_only=True)
class RestoreChannelCliCommand(ArchiveChannelCliCommand, declared_name="restore-channel"):
    help = "Restore archived channel"
    archived: Annotated[
        bool, TargetBound(), Argument(flags=("--archived",))
    ] = False

    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        return cls
