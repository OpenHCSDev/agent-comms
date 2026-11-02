from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.checkout_publication import PublishingCheckout
from agent_comms.cli_command import CliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller


@dataclass(frozen=True, kw_only=True)
class HomeCheckoutCliCommand(CliCommand):
    """The calling thread's selected launch checkout supplies the canonical target."""

    caller: Annotated[ThreadCaller, Normalize(FieldCodec.encode),
                      Argument(flags=("--from",), options={"type": ThreadCaller})]

    def home(self, ctx: Comms) -> tuple[Thread, PublishingCheckout]:
        thread = ctx.registry.require(self.caller.own_thread())
        checkout = PublishingCheckout.checked(thread.project_path)
        ctx.registry.storage_rule.require_destination(thread, checkout.path)
        return thread, checkout
