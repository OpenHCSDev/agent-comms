"""Antigravity ACP wire models and `session/update` decoding into Core's nominal AgentEvents."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from pydantic import ConfigDict, Field, JsonValue

from agent_comms import agent_events as events
from agent_comms.agent_backend import ConvertedEvent, UnconvertedEvent
from agent_comms.cli_bridges.wire_models import DeclaredWireFamily, WireModel
from agent_comms.config_option import CatalogChoice
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.runtime_observations import ContextInfo
from agent_comms.execution.turn_phase import (
    ModelWaitPhase,
    ReplyingPhase,
    ThinkingPhase,
    TurnPhase,
)
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import NominalText, ToolCallId
from agent_comms.pi_counts import ReportedCount, UnreportedCount


class AcpConfigKey(NominalText):
    """Nominal ACP configuration option key (`model`, `thought_level`, ...)."""


class AcpWireModel(WireModel):
    """Permissive-extras ACP wire record validated once at the JSON-RPC boundary."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, defer_build=True)


class AcpLeafContent(AcpWireModel):
    """Inner leaf text block inside an ACP content envelope."""

    type: str = ""
    text: str = ""


class AcpContentBlock(AcpWireModel):
    """One ACP content block (`{type: 'text', text: ...}` or `{type: 'content', content: ...}`)."""

    type: str = ""
    text: str = ""
    content: AcpLeafContent = Field(default_factory=AcpLeafContent)

    def extract_text(self) -> str:
        return self.content.text or self.text


class AcpConfigChoiceWire(AcpWireModel):
    """One selectable option value advertised inside an ACP `configOptions` entry."""

    value: str = ""
    name: str = ""
    description: str = ""

    def to_catalog_choice(self) -> CatalogChoice:
        return CatalogChoice(
            name=self.name or self.value,
            value=self.value,
            description=self.description,
        )


class AcpConfigOptionWire(AcpWireModel):
    """One ACP session configuration option (`model`, `thought_level`, `mode`, ...)."""

    id: str = ""
    current_value: str = Field(default="", alias="currentValue")
    options: list[AcpConfigChoiceWire] = Field(default_factory=list)


@dataclass
class AcpDescriptorBuilder:
    """Mutable collector populated polymorphically by `AcpConfigOptionKind` members."""

    current_model: str = ""
    thought_level: str = ""
    models: list[CatalogChoice] = field(default_factory=list)
    thinking_levels: list[str] = field(default_factory=list)


_BACKGROUND_TASK_ID_RE = re.compile(r"background task with task id:\s*(\S+)", re.IGNORECASE)


@dataclass
class AcpTurnStreamState:
    """Per-turn mutable correlation state while streaming one ACP `session/prompt`."""

    text_chunks: list[str] = field(default_factory=list)
    pending_progress_chunks: list[str] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)
    open_tools: set[str] = field(default_factory=set)
    background_tasks: set[str] = field(default_factory=set)
    phase: TurnPhase = field(default_factory=ModelWaitPhase)
    current_model: str = ""
    thought_level: str = ""

    @property
    def full_text(self) -> str:
        return "".join(self.text_chunks)

    def append_text(self, text: str) -> None:
        self.text_chunks.append(text)
        self.pending_progress_chunks.append(text)

    def record_tool_output(self, output: str) -> None:
        for match in _BACKGROUND_TASK_ID_RE.finditer(output):
            self.background_tasks.add(match.group(1).rstrip(".,;:)"))

    @property
    def has_quiet_transition(self) -> bool:
        return (
            not self.open_tools
            and self.phase.quiet_phase(background_tasks=tuple(sorted(self.background_tasks)))
            != self.phase
        )

    def observe_quiet_silence(self) -> TurnPhase | None:
        """Move a silent post-reply turn into BackgroundTaskWaitPhase once chunks stop."""
        if self.open_tools:
            return None
        next_phase = self.phase.quiet_phase(background_tasks=tuple(sorted(self.background_tasks)))
        if next_phase == self.phase:
            return None
        self.phase = next_phase
        return next_phase

    def commit_progress(self) -> Iterator[AgentEvent]:
        if not self.pending_progress_chunks:
            return
        progress = "".join(self.pending_progress_chunks)
        if not progress.strip():
            return
        self.pending_progress_chunks.clear()
        yield events.CommittedProgress(text=progress)


class AcpConfigOptionKind(DeclaredFamily, affix="AcpConfigOption"):
    """Nominal ACP configuration option identity shared by session init and live updates."""

    @classmethod
    def of(cls, option_key: AcpConfigKey) -> type[AcpConfigOptionKind]:
        return cls.member_or_default(str(option_key), OtherAcpConfigOption)

    @classmethod
    def apply_to_descriptor(
        cls,
        option: AcpConfigOptionWire,
        builder: AcpDescriptorBuilder,
    ) -> None:
        del option, builder

    @classmethod
    def apply_to_turn(
        cls,
        option: AcpConfigOptionWire,
        state: AcpTurnStreamState,
    ) -> None:
        del option, state


class ModelAcpConfigOption(AcpConfigOptionKind, declared_name="model"):
    @classmethod
    def apply_to_descriptor(
        cls,
        option: AcpConfigOptionWire,
        builder: AcpDescriptorBuilder,
    ) -> None:
        if option.current_value:
            builder.current_model = option.current_value
        for choice in option.options:
            if choice.value:
                builder.models.append(choice.to_catalog_choice())

    @classmethod
    def apply_to_turn(
        cls,
        option: AcpConfigOptionWire,
        state: AcpTurnStreamState,
    ) -> None:
        if option.current_value:
            state.current_model = option.current_value


class ThoughtLevelAcpConfigOption(AcpConfigOptionKind, declared_name="thought_level"):
    @classmethod
    def apply_to_descriptor(
        cls,
        option: AcpConfigOptionWire,
        builder: AcpDescriptorBuilder,
    ) -> None:
        if option.current_value:
            builder.thought_level = option.current_value
        for choice in option.options:
            if choice.value:
                builder.thinking_levels.append(choice.value)

    @classmethod
    def apply_to_turn(
        cls,
        option: AcpConfigOptionWire,
        state: AcpTurnStreamState,
    ) -> None:
        if option.current_value:
            state.thought_level = option.current_value


class OtherAcpConfigOption(AcpConfigOptionKind, declared_name="other"):
    pass


class AcpToolStatus(DeclaredFamily, affix="ToolStatus"):
    """Nominal status of an ACP `tool_call_update` notification."""

    @classmethod
    def of(cls, raw: str) -> type[AcpToolStatus]:
        return cls.member_or_default(raw, InProgressToolStatus)

    @classmethod
    def emit(
        cls,
        state: AcpTurnStreamState,
        call_id: ToolCallId,
        name: str,
        output: str,
    ) -> Iterator[AgentEvent]:
        del state
        yield events.ToolProgress(id=call_id, name=name, output=output)


class PendingToolStatus(AcpToolStatus, declared_name="pending"):
    pass


class InProgressToolStatus(AcpToolStatus, declared_name="in_progress"):
    pass


class CompletedToolStatus(AcpToolStatus, declared_name="completed"):
    @classmethod
    def emit(
        cls,
        state: AcpTurnStreamState,
        call_id: ToolCallId,
        name: str,
        output: str,
    ) -> Iterator[AgentEvent]:
        yield from state.commit_progress()
        state.record_tool_output(output)
        state.open_tools.discard(str(call_id))
        state.phase = state.phase.tool_ended(call_id)
        yield events.ToolEnd(id=call_id, name=name, ok=True, output=output)


class FailedToolStatus(AcpToolStatus, declared_name="failed"):
    @classmethod
    def emit(
        cls,
        state: AcpTurnStreamState,
        call_id: ToolCallId,
        name: str,
        output: str,
    ) -> Iterator[AgentEvent]:
        yield from state.commit_progress()
        state.open_tools.discard(str(call_id))
        state.phase = state.phase.tool_ended(call_id)
        yield events.ToolEnd(id=call_id, name=name, ok=False, output=output)


class AntigravityAcpUpdate(ConvertedEvent, DeclaredWireFamily, affix="AcpUpdate"):
    """One ACP `session/update` notification kind mapped into Core `AgentEvent` records."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, defer_build=True)
    session_update: str = Field(default="", alias="sessionUpdate")

    @classmethod
    def decode_wire(cls, payload: Mapping[str, object]) -> AntigravityAcpUpdate:
        header = cls.model_validate(payload)
        member = cls.member_or_default(header.session_update, UnknownAcpUpdate)
        return member.model_validate(payload)

    @classmethod
    def decode(
        cls,
        update: Mapping[str, object],
        state: AcpTurnStreamState,
    ) -> tuple[AgentEvent, ...]:
        return tuple(cls.decode_wire(update).convert(state))

    def convert(self, state: AcpTurnStreamState) -> Iterator[AgentEvent]:
        del state
        return iter(())


class AgentMessageChunkAcpUpdate(AntigravityAcpUpdate, declared_name="agent_message_chunk"):
    content: AcpContentBlock = Field(default_factory=AcpContentBlock)

    def convert(self, state: AcpTurnStreamState) -> Iterator[AgentEvent]:
        text = self.content.extract_text()
        if text:
            state.phase = ReplyingPhase()
            state.append_text(text)
            yield events.Chunk(text=text)


class AgentThoughtChunkAcpUpdate(AntigravityAcpUpdate, declared_name="agent_thought_chunk"):
    content: AcpContentBlock = Field(default_factory=AcpContentBlock)

    def convert(self, state: AcpTurnStreamState) -> Iterator[AgentEvent]:
        text = self.content.extract_text()
        if text:
            yield from state.commit_progress()
            state.phase = ThinkingPhase()
            yield events.Thinking(text=text)


class UserMessageChunkAcpUpdate(AntigravityAcpUpdate, declared_name="user_message_chunk"):
    pass


class ToolCallAcpUpdate(AntigravityAcpUpdate, declared_name="tool_call"):
    tool_call_key: str = Field(default="agy-tool", alias="toolCallId")
    title: str = ""
    kind: str = ""
    name: str = ""
    raw_input: JsonValue = Field(default="", alias="rawInput")

    def convert(self, state: AcpTurnStreamState) -> Iterator[AgentEvent]:
        yield from state.commit_progress()
        call_key = self.tool_call_key or "agy-tool"
        call_id = ToolCallId(call_key)
        title = self.title or self.kind or "tool"
        name = self.name or title
        state.tool_names[call_key] = name
        state.open_tools.add(call_key)
        start = events.ToolStart.from_call(call_id, name, self.raw_input)
        if title and not start.title:
            start = events.ToolStart(
                id=start.id,
                name=start.name,
                title=title,
                args=start.args,
                kind=start.kind,
            )
        state.phase = state.phase.tool_started(call_id, start.title or name)
        yield start


def _format_raw_output(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if not value:
        return ""
    return json.dumps(value)


class ToolCallUpdateAcpUpdate(AntigravityAcpUpdate, declared_name="tool_call_update"):
    tool_call_key: str = Field(default="agy-tool", alias="toolCallId")
    name: str = ""
    title: str = ""
    status: str = ""
    content: list[AcpContentBlock] = Field(default_factory=list)
    raw_output: JsonValue = Field(default="", alias="rawOutput")

    def convert(self, state: AcpTurnStreamState) -> Iterator[AgentEvent]:
        call_key = self.tool_call_key or "agy-tool"
        candidates = (state.tool_names.get(call_key), self.name, self.title)
        name = next((candidate for candidate in candidates if candidate), "tool")
        parts = [block.extract_text() for block in self.content]
        output = "\n".join(part for part in parts if part) or _format_raw_output(self.raw_output)
        yield from AcpToolStatus.of(self.status).emit(state, ToolCallId(call_key), name, output)


class UsageUpdateAcpUpdate(AntigravityAcpUpdate, declared_name="usage_update"):
    used: int = 0
    size: int = 0

    def convert(self, state: AcpTurnStreamState) -> Iterator[AgentEvent]:
        if state.open_tools:
            yield from state.commit_progress()
        if self.size > 0:
            yield ContextInfo(
                context_used=ReportedCount(self.used),
                context_size=ReportedCount(self.size),
            )
        elif self.used > 0:
            yield ContextInfo(
                context_used=ReportedCount(self.used),
                context_size=UnreportedCount(),
            )


class ConfigOptionUpdateAcpUpdate(AntigravityAcpUpdate, declared_name="config_option_update"):
    config_options: list[AcpConfigOptionWire] = Field(default_factory=list, alias="configOptions")

    def convert(self, state: AcpTurnStreamState) -> Iterator[AgentEvent]:
        for option in self.config_options:
            AcpConfigOptionKind.of(AcpConfigKey(option.id)).apply_to_turn(option, state)
        return iter(())


class AvailableCommandsUpdateAcpUpdate(
    AntigravityAcpUpdate, declared_name="available_commands_update"
):
    pass


class SessionInfoUpdateAcpUpdate(AntigravityAcpUpdate, declared_name="session_info_update"):
    pass


class PlanAcpUpdate(AntigravityAcpUpdate, declared_name="plan"):
    pass


class CurrentModeUpdateAcpUpdate(AntigravityAcpUpdate, declared_name="current_mode_update"):
    pass


class UnknownAcpUpdate(UnconvertedEvent, AntigravityAcpUpdate):
    """Forward-compatible fallback for newly introduced Antigravity ACP update kinds."""


class AcpPromptFrame(ABC):
    """One nominal frame streamed during an Antigravity ACP `session/prompt` call."""

    @abstractmethod
    def apply(
        self,
        state: AcpTurnStreamState,
        on_settled: Callable[[AcpTurnStreamState], None],
    ) -> Iterator[AgentEvent]: ...


@dataclass(frozen=True)
class AcpUpdateFrame(AcpPromptFrame):
    """A decoded `session/update` notification belonging to the active prompt."""

    update: AntigravityAcpUpdate

    def apply(
        self,
        state: AcpTurnStreamState,
        on_settled: Callable[[AcpTurnStreamState], None],
    ) -> Iterator[AgentEvent]:
        del on_settled
        yield from self.update.convert(state)


@dataclass(frozen=True)
class AcpResultFrame(AcpPromptFrame):
    """The terminal `session/prompt` JSON-RPC response."""

    stop_reason: str = ""

    def apply(
        self,
        state: AcpTurnStreamState,
        on_settled: Callable[[AcpTurnStreamState], None],
    ) -> Iterator[AgentEvent]:
        on_settled(state)
        return iter(())


ANTIGRAVITY_ACP_EVENT_KINDS: frozenset[str] = frozenset(
    kind
    for member in AntigravityAcpUpdate.members_with(AntigravityAcpUpdate)
    for kind in member.known_kinds()
)
