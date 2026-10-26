from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import WorktreeArgument
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Editor
from agent_comms.target_fields import ChoiceTargetField
from agent_comms.working_memory_labels import ModelLabel
from agent_comms.working_memory_questions import SpanAnswer
from dataclasses import dataclass
from typing import Annotated
import json


@dataclass(frozen=True, kw_only=True)
class CorrectAnnotationCliCommand(WorktreeArgument, CliCommand, declared_name="correct-annotation"):
    help = "Correct one original stored model answer through the human identity owner"
    label: Annotated[
        ModelLabel, Argument(flags=("--label",), options={"type": json.loads})
    ]
    answer: Annotated[
        type[SpanAnswer],
        Editor(ChoiceTargetField),
        Argument(flags=("--answer",)),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        author = ctx.messaging.user_identity(self.worktree).incarnation
        snapshot = ctx.registry.snapshot()
        with Coordination(str(CoordinationStore.path_in(ctx.root))) as store:
            return store.annotations.correct(self.label, self.answer, author, snapshot)
