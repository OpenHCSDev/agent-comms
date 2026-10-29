from __future__ import annotations

from agent_comms.cli_support.publishing import PublishingCliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.send_request import SendRequest
from agent_comms.send_result import MessageSentResult
from dataclasses import dataclass
from dataclasses import field
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class SendCliCommand(SendRequest, PublishingCliCommand):
    # A bare annotation would inherit SendRequest's default; field() keeps --from required.
    sender: Annotated[str, Argument(flags=("--from",))] = field()

    def publish(self, ctx: Comms) -> MessageSentResult:
        return MessageSentResult(self.sent(ctx))
