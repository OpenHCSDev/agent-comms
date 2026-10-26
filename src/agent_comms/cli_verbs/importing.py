"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_bridges.history import History
from agent_comms.cli_commands import _tags
from agent_comms.cli_verbs.conversation_limits import ConversationLimitsCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Normalize
from agent_comms.importing import ThreadImported
from agent_comms.thread_management import ImportDirectory
from agent_comms.thread_management import SourceDirectory
from agent_comms.threads import KeptModel
from agent_comms.threads import ModelChoice
from dataclasses import dataclass
from typing import Annotated
import dataclasses


@dataclass(frozen=True, kw_only=True)
class ImportingCliCommand(ConversationLimitsCliCommand):
    """Where an imported thread works and how much of its source conversation it keeps."""

    worktree: Annotated[
        ImportDirectory,
        Argument(
            flags=("--worktree",), options={"help": "Override the source project directory"}
        ),
    ] = SourceDirectory()
    model: Annotated[
        ModelChoice,
        Argument(
            flags=("--model",),
            options={"help": "Target Pi provider/model; not inferred from foreign model IDs"},
        ),
    ] = KeptModel()
    tags: Annotated[
        frozenset[str],
        Normalize(_tags),
        Argument(flags=("--tags",)),
    ] = dataclasses.field(default_factory=frozenset)

    def imported(self, ctx: Comms, history: History, name: str) -> ThreadImported:
        return ctx.threads.import_thread(
            history, name=name, worktree=self.worktree, model=self.model, tags=self.tags,
            limits=self.limits,
        )
