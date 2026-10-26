"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import ToolCatalogResult
from agent_comms.field_codec import Argument
from agent_comms.field_codec import FieldCodec
from agent_comms.field_codec import Normalize
from agent_comms.native_tools import NativeRequestTools
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.tool_caller import ThreadCaller
from dataclasses import dataclass
from typing import Annotated


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
