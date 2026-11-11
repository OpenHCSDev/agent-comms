from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from agent_comms.execution.native_failure_reads import NativeResponseErrorRead
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.pi_wire import (
    OpenPayload,
    PiField,
    PiOmitted,
    PiPayload,
    PiReported,
    UnknownPayload,
)
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import PresentMember, ToolCallId, WireName
from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    ThinkingTranscript,
    ToolStartTranscript,
    UserTranscript,
)
from agent_comms.transport.tool_declaration import ToolDeclaration


@dataclass(frozen=True)
class ProviderDiagnosticError(PiPayload, DeclaredFamily, affix="ProviderDiagnosticError"):
    message: str
    name: PiField[ReportedText | PiOmitted] = PiOmitted()

    @classmethod
    def wire_member(cls, value: dict[str, object]) -> type[ProviderDiagnosticError]:
        return CodedProviderDiagnosticError if "code" in value else CodelessProviderDiagnosticError

    @property
    def description(self) -> str:
        return self.name.text_or("Provider transport error")


@dataclass(frozen=True)
class CodelessProviderDiagnosticError(ProviderDiagnosticError):
    """Pi reported a provider error without a code."""


@dataclass(frozen=True, kw_only=True)
class CodedProviderDiagnosticError(ProviderDiagnosticError):
    code: int | str

    @property
    def description(self) -> str:
        return f"{super().description} (code {self.code})"


@dataclass(frozen=True)
class ReportedText(PiReported[str], NativeResponseErrorRead):
    value: str

    @property
    def message_parts(self) -> tuple[PiContent, ...]:
        return (TextContent(self.value),)

    def rendered_text(self, separator: str) -> str:
        return self.value

    def text_or[D](self, default: D) -> str | D:
        """Pi's empty text falls back exactly as its omitted text does."""
        return self.value or default


@dataclass(frozen=True)
class PiContent(PresentMember, OpenPayload, affix="Content"):
    def text_parts(self) -> tuple[str, ...]:
        return ()

    def with_display_text(self, text: str) -> PiContent | PiOmitted:
        """Keep non-text content beside the message's replacement display block."""
        return self

    def tool_calls(self) -> tuple[ToolCallContent, ...]:
        return ()

    def preserve_evidence(self, raw: dict) -> PiContent:
        """Keep unrepresented native fields opaque, never equivalent to plain text."""
        return self if self.to_wire() == raw else UnknownContent(payload=raw)

    def user_transcript(self, position: int, timestamp: float):
        return []

    def assistant_transcript(self, position: int, timestamp: float):
        return []

    text: ClassVar[str] = ""
    tool_round_allowed: ClassVar[bool] = False

    @property
    def final_text(self) -> str:
        raise NativePiUnavailable("Native Pi assistant returned non-text content")


@dataclass(frozen=True)
class TextContent(PiContent):
    def text_parts(self) -> tuple[str, ...]:
        return (self.text,)

    def with_display_text(self, text: str) -> PiOmitted:
        """The message places the display text once, replacing every original block."""
        return PiOmitted()

    def user_transcript(self, position: int, timestamp: float):
        return [UserTranscript(self.text, position=position, timestamp=timestamp)]

    def assistant_transcript(self, position: int, timestamp: float):
        return [AssistantTranscript(self.text, position=position, timestamp=timestamp)]

    text: str = field()
    text_signature: Annotated[
        PiField[str | PiOmitted], WireName("textSignature")
    ] = PiOmitted()
    tool_round_allowed = True

    @property
    def final_text(self) -> str:
        return self.text


@dataclass(frozen=True)
class ThinkingContent(PiContent):
    def assistant_transcript(self, position: int, timestamp: float):
        return [ThinkingTranscript(self.thinking, position=position, timestamp=timestamp)]

    thinking: str = ""
    thinking_signature: Annotated[
        PiField[str | PiOmitted], WireName("thinkingSignature")
    ] = PiOmitted()
    tool_round_allowed = True

    @property
    def final_text(self) -> str:
        return ""


@dataclass(frozen=True)
class ToolCallContent(PiContent, declared_name="toolCall"):
    def tool_calls(self) -> tuple[ToolCallContent, ...]:
        return (self,)

    def assistant_transcript(self, position: int, timestamp: float):
        return [
            ToolStartTranscript(position=position, timestamp=timestamp, tool_call_id=self.id, tool_name=self.name, raw_input=self.arguments,
                                tool=ToolDeclaration.record(self.name, self.arguments, call_id=self.id))
        ]

    id: ToolCallId
    name: str
    arguments: object
    tool_round_allowed = True


@dataclass(frozen=True)
class ImageContent(PiContent):
    def user_transcript(self, position: int, timestamp: float):
        return [UserTranscript(f"[Image attachment: {self.mime_type or 'image'}]", position=position, timestamp=timestamp)]

    data: str
    mime_type: Annotated[PiField[str], WireName("mimeType")] = None


@dataclass(frozen=True)
class UnknownContent(UnknownPayload, PiContent):
    pass
