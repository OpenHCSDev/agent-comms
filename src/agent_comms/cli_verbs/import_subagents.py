from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_bridges.claude_transcript import ClaudeTranscript
from agent_comms.cli_support.importing import ImportingCliCommand
from agent_comms.cli_verbs.cli_commands import SubagentsImportedResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, PathText
from agent_comms_hooks import SubagentId


@dataclass(frozen=True, kw_only=True)
class ImportSubagentsCliCommand(ImportingCliCommand, declared_name="import-subagents"):
    help = "Import every subagent of a stored Claude Code session, each as a stopped thread"
    session: Annotated[
        Path,
        PathText,
        Argument(
            flags=("--session",), options={"help": "The Claude Code session JSONL"}
        ),
    ]
    prefix: Annotated[
        str,
        Argument(
            flags=("--prefix",),
            options={"help": "Prepended to each subagent id to name its thread"},
        ),
    ] = ""

    def apply(self, ctx: Comms) -> SubagentsImportedResult:
        return SubagentsImportedResult(tuple(
            self.imported(ctx, ClaudeTranscript(path), self.prefix + SubagentId.of_transcript(path))
            for path in ClaudeTranscript.subagents(self.session.expanduser().resolve())))
