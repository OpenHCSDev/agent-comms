from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.cli_verbs.target_fields import ChoiceTargetField
from agent_comms.comms import Comms
from agent_comms.coordination.working_memory_annotations import WorkingMemoryAnnotations
from agent_comms.nominal.field_codec import Argument, Editor
from agent_comms.working_memory.working_memory_labels import ClassifierVersion, QuestionVersion


@dataclass(frozen=True, kw_only=True)
class AnnotationsCalibrationCliCommand(CliCommand, declared_name="annotations"):
    help = (
        "Report human-reviewed accuracy and probability frequencies for exact annotation versions"
    )
    operation: Annotated[
        Literal["calibration"],
        Editor(ChoiceTargetField),
        Argument(flags=("operation",), options={"help": "calibration"}),
    ]
    question: Annotated[
        QuestionVersion,
        Argument(
            flags=("--question",),
            options={
                "type": json.loads,
                "help": "JSON of the original question member and version digest",
            },
        ),
    ]
    classifier: Annotated[
        ClassifierVersion,
        Argument(
            flags=("--classifier",),
            options={
                "type": json.loads,
                "help": "JSON of the original classifier member and pinned release",
            },
        ),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        return WorkingMemoryAnnotations.calibration(
            ctx.registry.store.coordinator_reader, self.question, self.classifier)
