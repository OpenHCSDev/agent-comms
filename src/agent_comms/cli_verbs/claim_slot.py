from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.thread_text import SubagentRole
from dataclasses import dataclass
from typing import Annotated

from ..cli_result import RegisteredResult


@dataclass(frozen=True, kw_only=True)
class ClaimSlotCliCommand(CliCommand[RegisteredResult], declared_name="claim-slot"):
    help = "Seat a subagent in the first vacant slot of its role under its parent thread"
    parent: Annotated[
        str, Argument(flags=("--parent",), options={"help": "The parent session's thread"})
    ]
    role: Annotated[
        SubagentRole,
        Argument(flags=("--role",), options={"help": "The harness's agent type"}),
    ]

    def apply(self, ctx: Comms) -> RegisteredResult:
        return RegisteredResult(ctx.registry.claim_slot(self.parent, self.role, ctx.goals).name)
