from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from agent_comms.cli_support.publishing import PublishingCliCommand
from agent_comms.cli_verbs.send_request import SendRequest
from agent_comms.comms import Comms
from agent_comms.messaging.send_result import MessageSentResult
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True, kw_only=True)
class SendCliCommand(SendRequest, PublishingCliCommand):
    # A bare annotation would inherit SendRequest's default; field() keeps --from required.
    sender: Annotated[str, Argument(flags=("--from",))] = field()

    def publish(self, ctx: Comms) -> MessageSentResult:
        return MessageSentResult(self.sent(ctx))
