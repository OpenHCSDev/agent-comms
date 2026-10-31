from __future__ import annotations

from agent_comms.cli_verbs.pin_constraint import PinConstraintCliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from agent_comms.turn_context import ContextSegment
from dataclasses import dataclass
from typing import Annotated
import json


@dataclass(frozen=True, kw_only=True)
class ApproveAnnotationsCliCommand(PinConstraintCliCommand, declared_name="approve-annotations"):
    help = "Approve a pinned external classifier using original human wording and an hourly request budget"
    per_hour: Annotated[
        int,
        Argument(
            flags=("--per-hour",),
            options={"help": "Maximum classifier requests in the rolling hour"},
        ),
    ]
    segments: Annotated[
        tuple[type[ContextSegment], ...],
        Argument(
            flags=("--segments",),
            options={
                "type": json.loads,
                "help": "JSON list of original segment kinds permitted for disclosure",
            },
        ),
    ]

    def pin_original(self, ctx: Comms, source: MessageReference) -> Message:
        return ctx.messaging.approve_annotations(self.thread, source, worktree=self.worktree,
                                                per_hour=self.per_hour, segments=self.segments)
