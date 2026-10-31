from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import ToolCatalogResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller
from agent_comms.tool_declaration import NativeRequestTools


@dataclass(frozen=True, kw_only=True)
class ToolsCliCommand(CliCommand):
    help = "Emit the shared adapter tool catalog"
    name: Annotated[
        ThreadCaller | NoThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(flags=("--name",), options={"type": ThreadCaller, "help": "Calling thread"}),
    ] = NoThreadCaller()

    def apply(self, ctx: Comms) -> ToolCatalogResult:
        return self.name.catalog(ctx, tuple(NativeRequestTools.catalog()))
