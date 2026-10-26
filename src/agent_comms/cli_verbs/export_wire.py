from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import _duration_seconds
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.exporting import EverythingScope
from agent_comms.exporting import FullLimit
from agent_comms.exporting import JsonlFormat
from agent_comms.exporting import LimitFlag
from agent_comms.exporting import ScopeFlag
from agent_comms.exporting import SelectableWireExportFormat
from agent_comms.exporting import UnsetLimitFlag
from agent_comms.exporting import UnsetScopeFlag
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Editor
from agent_comms.field_codec import NamedArgumentGroup
from agent_comms.target_fields import ChoiceTargetField
from dataclasses import dataclass
from typing import Annotated
import time


@dataclass(frozen=True, kw_only=True)
class ExportWireCliCommand(CliCommand, declared_name="export-wire"):
    help = "Export durable IRC/wire message history"
    output: Annotated[
        str, Argument(flags=("--output",), options={"help": "Destination file"})
    ]
    format: Annotated[
        SelectableWireExportFormat,
        Editor(ChoiceTargetField),
        Argument(flags=("--format",)),
    ] = JsonlFormat()
    everything: Annotated[
        bool,
        Argument(
            flags=("--everything",),
            group=NamedArgumentGroup("scope"),
            options={"help": "Every wire message"},
        ),
    ] = False
    channel: Annotated[
        ScopeFlag,
        Argument(
            flags=("--channel",),
            group=NamedArgumentGroup("scope"),
            options={"help": "One target-owned #channel"},
        ),
    ] = UnsetScopeFlag()
    dm: Annotated[
        ScopeFlag,
        Argument(
            flags=("--dm",),
            group=NamedArgumentGroup("scope"),
            options={"nargs": 2, "metavar": ("FIRST", "SECOND"), "help": "One canonical DM"},
        ),
    ] = UnsetScopeFlag()
    full: Annotated[
        bool,
        Argument(
            flags=("--full",),
            group=NamedArgumentGroup("limit"),
            options={"help": "Export without a size/time cap"},
        ),
    ] = False
    max_bytes: Annotated[
        LimitFlag,
        Argument(
            flags=("--max-bytes",),
            group=NamedArgumentGroup("limit"),
            options={"type": int, "help": "Hard UTF-8 artifact byte ceiling"},
        ),
    ] = UnsetLimitFlag()
    last: Annotated[
        LimitFlag,
        Argument(
            flags=("--last",),
            group=NamedArgumentGroup("limit"),
            options={
                "type": _duration_seconds,
                "metavar": "DURATION",
                "help": "Recent window, e.g. 24h",
            },
        ),
    ] = UnsetLimitFlag()
    overwrite: Annotated[
        bool,
        Argument(
            flags=("--overwrite",), options={"help": "Replace an existing output"}
        ),
    ] = False

    def apply(self, ctx: Comms) -> CliResult:
        started_at = time.time()
        # The required argument groups hold exactly one flag each; --everything and --full
        # are what remains when no other flag of their group names a choice.
        exported = ctx.views.export_wire(
            self.output,
            format=self.format,
            scope=self.channel.chosen_or(self.dm.chosen_or(EverythingScope())),
            limit=self.max_bytes.chosen_or(self.last.chosen_or(FullLimit(), started_at), started_at),
            overwrite=self.overwrite,
            export_started_at=started_at,
        )
        return exported
