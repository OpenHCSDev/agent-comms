"""Tool reply carrying a published message's transcript record and wording-correction status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from agent_comms.messaging.send_result import CorrectionResult, SendResult
from agent_comms.nominal.field_codec import MessageId, WireName
from agent_comms.transcript.transcript_events import SentTranscript
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True)
class MessagePublished(ToolResult):
    """The Pi adapter decodes this reply as PublishedMessageToolDetails by its agentCommsKind."""

    id: MessageId
    message: SentTranscript
    correction: CorrectionResult
    agent_comms_kind: Annotated[
        Literal["published_message"], WireName("agentCommsKind")
    ] = "published_message"

    @classmethod
    def from_send(cls, result: SendResult) -> MessagePublished:
        return cls(
            result.message.message_id,
            SentTranscript.from_message(result.message),
            result.correction,
        )

    def compact_text(self) -> str:
        targets = ", ".join(self.message.routing.reply.targets)
        line = f"Sent {self.message.source.name} -> {targets}"
        notices = self.correction.notices(self.id)
        return "\n".join((line, *notices)) if notices else line
