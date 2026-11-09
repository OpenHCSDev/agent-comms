from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.history_views import CallingThreadTarget, NamedThreadTarget, PollTarget
from agent_comms.nominal.field_codec import Argument, Normalize


@dataclass(frozen=True, kw_only=True)
class PollCliCommand(CliCommand):
    help = "Status snapshot: thread, inbox, ledger, peers"
    # --thread names a thread; without it the poll is the calling thread's. Both parse to records.
    thread: Annotated[
        PollTarget,
        Normalize(dict),
        Argument(
            flags=("--thread",),
            options={"action": "store", "type": NamedThreadTarget.argument},
        ),
    ] = dataclasses.field(default_factory=CallingThreadTarget)

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.views.poll(self.thread)
