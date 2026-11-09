from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import WorktreeArgument
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.cli_verbs.target_fields import ChoiceTargetField
from agent_comms.comms import Comms
from agent_comms.coordination.annotation_correction import CorrectsAnnotation
from agent_comms.nominal.field_codec import Argument, Editor
from agent_comms.working_memory.working_memory_labels import ModelLabel
from agent_comms.working_memory.working_memory_questions import SpanAnswer


@dataclass(frozen=True, kw_only=True)
class CorrectAnnotationCliCommand(
    CorrectsAnnotation, WorktreeArgument, CliCommand, declared_name="correct-annotation"
):
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
        return self.correct(ctx)
