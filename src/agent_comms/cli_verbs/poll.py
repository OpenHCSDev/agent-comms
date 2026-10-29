from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import Normalize
from agent_comms.history_views import CallingThreadTarget
from agent_comms.history_views import NamedThreadTarget
from agent_comms.history_views import PollTarget
from dataclasses import dataclass
from typing import Annotated
import dataclasses


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
