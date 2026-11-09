from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize
from agent_comms.projects.checkout_publication import PublishingCheckout
from agent_comms.registry.threads import Thread
from agent_comms.transport.tool_caller import ThreadCaller


@dataclass(frozen=True, kw_only=True)
class HomeCheckoutCliCommand(CliCommand):
    """Publication commands select a target without moving the caller's editing project."""

    caller: Annotated[ThreadCaller, Normalize(FieldCodec.encode),
                      Argument(flags=("--from",), options={"type": ThreadCaller})]

    checkout: Annotated[PublishingCheckout, Argument(flags=("--checkout",), options={
        "help": "Physical publication checkout of the selected project's repository",
    })]

    def home(self, ctx: Comms) -> tuple[Thread, PublishingCheckout]:
        thread = ctx.registry.require(self.caller.own_thread())
        checkout = self.checkout.qualified_for(thread.worktree)
        ctx.registry.storage_rule.require_destination(thread, checkout.path)
        return thread, checkout
