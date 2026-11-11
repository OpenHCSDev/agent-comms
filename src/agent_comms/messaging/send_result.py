"""A committed send and its independent post-delivery wording correction."""

from __future__ import annotations


from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated, Literal

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.messaging.message_send_reads import CommittedMessageRead
from agent_comms.messaging.messages import Message
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, MessageId, WireName
from agent_comms.transcript.transcript_events import SentTranscript
from agent_comms.transport.tool_result import ToolResult


class CorrectionResult(DeclaredFamily):
    @abstractmethod
    def notices(self, message_id: MessageId) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class CorrectionComplete(CorrectionResult, declared_name="complete"):
    def notices(self, message_id: MessageId) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class CorrectionFailed(CorrectionResult, declared_name="failed"):
    error: str

    def notices(self, message_id: MessageId) -> tuple[str, ...]:
        return (f"Message {message_id} was sent; wording correction failed: {self.error}",)


@dataclass(frozen=True)
class CorrectionUnreported(CorrectionResult, declared_name="unreported"):
    """Older retained tool replies did not report their correction outcome."""
    def notices(self, message_id: MessageId) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class SendResult(CommittedMessageRead):
    message: Message
    correction: CorrectionResult

    @property
    def notices(self) -> tuple[str, ...]:
        return self.correction.notices(self.message.message_id)


@dataclass(frozen=True)
class MessageSentResult(CliResult):
    sent: SendResult

    def to_wire(self) -> object:
        message_id = self.sent.message.message_id
        return {'id': message_id, 'message': f'Message {message_id} was sent',
                'correction': FieldCodec.encode(self.sent.correction),
                'notices': self.sent.notices}


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
        return cls(result.message.message_id, SentTranscript.from_message(result.message),
                   result.correction)
