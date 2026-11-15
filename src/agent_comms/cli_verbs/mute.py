"""Silence or restore the caller's own wake in the group chat for a bounded time.

Wake-all stays the default: a post pings every member. This verb only lets a member
opt their OWN wake out for a while; they still receive every message, they are just not
woken to triage it until the mute expires or they clear it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.waking.mute_ledger import MuteLedger


@dataclass(frozen=True)
class MutedResult(CliResult):
    muter: str
    muted_until: float


@dataclass(frozen=True, kw_only=True)
class MuteCliCommand(CliCommand, declared_name="mute"):
    help = ("Silence your own wake in the group chat for --minutes; you still receive every "
            "message, you are just not woken to triage it. --clear restores it.")
    caller: Annotated[ThreadCaller, Normalize(FieldCodec.encode),
                      Argument(flags=("--from",), options={"type": ThreadCaller})]
    minutes: Annotated[float, Argument(flags=("--minutes",))] = 60.0
    clear: Annotated[bool, Argument(flags=("--clear",))] = False

    def apply(self, ctx: Comms) -> MutedResult:
        thread = ctx.registry.require(self.caller.own_thread())
        lookup = thread.incarnation.lookup
        ledger = MuteLedger.at(ctx.root)
        if self.clear:
            ledger.clear(lookup)
            return MutedResult(thread.name, 0.0)
        until = time.time() + self.minutes * 60
        ledger.set(lookup, until)
        return MutedResult(thread.name, until)
