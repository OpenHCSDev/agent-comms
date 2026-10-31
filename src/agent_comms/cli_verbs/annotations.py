from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import Editor
from agent_comms.target_fields import ChoiceTargetField
from agent_comms.working_memory_annotations import WorkingMemoryAnnotations
from agent_comms.working_memory_labels import ClassifierVersion
from agent_comms.working_memory_labels import QuestionVersion
from dataclasses import dataclass
from typing import Annotated
from typing import Literal
import json


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
