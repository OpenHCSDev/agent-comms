from __future__ import annotations

from agent_comms.cli_bridges.history import AnySession
from agent_comms.cli_bridges.history import ExpectedSession
from agent_comms.cli_bridges.history import OfflineHistory
from agent_comms.cli_result import CliResult
from agent_comms.cli_support.importing import ImportingCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Editor
from agent_comms.field_codec import PathText
from agent_comms.harness_install import Harness
from agent_comms.target_fields import ChoiceTargetField
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ImportThreadCliCommand(ImportingCliCommand, declared_name="import-thread"):
    help = "Import a stored session of the --harness as a stopped thread"
    harness: Annotated[
        type[Harness],
        Editor(ChoiceTargetField),
        Argument(flags=("--harness",)),
    ]
    source: Annotated[
        Path,
        PathText,
        Argument(
            flags=("--source",),
            options={
                "help": "The harness's stored session: session JSONL, OpenCode export or opencode.db"
            },
        ),
    ]
    session: Annotated[
        ExpectedSession,
        Argument(
            flags=("--session-id",), options={"help": "Required for OpenCode database imports"}
        ),
    ] = AnySession()
    name: Annotated[
        str,
        Argument(flags=("--name",), options={"help": "New agent-comms thread name"}),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        return self.imported(ctx, OfflineHistory.of(
            self.harness, self.source.expanduser().resolve(), self.session), self.name)
