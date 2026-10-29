from __future__ import annotations

from agent_comms.comms import Comms

from agent_comms import cli as comms_cli
from agent_comms.cli_command import CliCommand
from agent_comms.cli_options import SingleArgument
from agent_comms.field_codec import Argument
from agent_comms.field_codec import FieldCodec
from agent_comms.field_codec import Normalize
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import ToolReplyResult
from agent_comms.tools import invoke_tool
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
import dataclasses
import json
import sys


@dataclass(frozen=True, kw_only=True)
class InvokeCliCommand(CliCommand[ToolReplyResult], declared_name="tool"):
    help = "Invoke one declared adapter tool, as --from THREAD when given"
    caller: Annotated[
        ThreadCaller | NoThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(
            flags=("--from",),
            options={"type": ThreadCaller, "action": SingleArgument,
                     "help": "Calling thread; omit for tools that need no thread"},
        ),
    ] = NoThreadCaller()
    tool: Annotated[str, Argument(flags=("--tool",))]
    arguments: Annotated[
        dict[str, object],
        Argument(
            flags=("--arguments",),
            options={"help": "Tool arguments as a JSON object"},
        ),
    ] = dataclasses.field(default_factory=dict)

    def argv(self, root: Path) -> tuple[str, ...]:
        """The CLI boundary owns its module, grammar and argument serialization."""
        return (sys.executable, '-m', comms_cli.__name__, '--root', str(root),
                self.declared_name, *self.caller.from_flag(), '--tool', self.tool,
                '--arguments', json.dumps(self.arguments))

    def apply(self, ctx: Comms) -> ToolReplyResult:
        return ToolReplyResult(invoke_tool(ctx, self.tool, self.arguments, caller=self.caller))
