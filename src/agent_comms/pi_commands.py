"""Typed outbound Pi commands and command-owned response semantics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from . import agent_events as events
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .image_inputs import ImageInput
from .owner_compaction_prepare import NativeWitness
from .pi_payloads import (
    CompactionData,
    EmptyData,
    ModelsData,
    PiResponseData,
    SessionStatsData,
    StateData,
    ThinkingLevelsData,
    UnknownData,
)
from .pi_summary_payloads import (
    CompactionSettingsData,
    SelectedModel,
    SelectedProbeData,
    SelectedSettings,
    SelectedSummaryData,
)

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import Response
    from .turn_inputs import ForwardedInput


class SessionSnapshot:
    """Reports the bound session identity before metadata projection."""


class MutatesSession:
    """A command may change the native session identity before its response."""

    async def steer(self, session: TurnSession, forwarded: ForwardedInput) -> bool:
        session.rejected_commands.append(
            events.Error(
                reason_code="steering_command_rejected",
                command=self.declared_name,
                id=self.id,
                text=f"Mid-turn {self.declared_name} is not supported.",
            )
        )
        session.rejected_signal.set()
        return True


@dataclass(frozen=True, kw_only=True)
class PiCommand(DeclaredFamily):
    response_payload: ClassVar[type[PiResponseData]] = EmptyData
    strict_response: ClassVar[bool] = False
    id: str | None = field(default=None, metadata={"wire_omit_default": True})

    async def steer(self, session: TurnSession, forwarded: ForwardedInput) -> bool:
        session.stdin.write(session.reader.encode(self))
        await session.stdin.drain()
        return True

    def to_rpc(self) -> dict[str, Any]:
        data = FieldCodec.encode(self)
        data["type"] = data.pop("kind")
        return data

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> PiCommand:
        data = dict(value)
        kind = data.pop("type", None)
        try:
            cls.decode(kind)
        except ValueError:
            return UnknownCommand(wire=value)
        member = cls.decode(kind)
        return FieldCodec.decode(cls, {"kind": kind, **member.decode_parameters(data)})

    @classmethod
    def decode_parameters(cls, data):
        return data

    @classmethod
    def response_owner(cls, name: str) -> type[PiCommand]:
        try:
            return cls.decode(name)
        except ValueError:
            return UnknownCommand

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class UnknownCommand(PiCommand):
    response_payload = UnknownData
    wire: dict[str, Any]

    def to_rpc(self) -> dict[str, Any]:
        return dict(self.wire)


@dataclass(frozen=True, kw_only=True)
class Prompt(PiCommand):
    async def steer(self, session: TurnSession, forwarded: ForwardedInput) -> bool:
        return await session.inputs.send_prompt(self, session, forwarded)

    input_id: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "inputId"}
    )
    message: str = field(default=None, metadata={"wire_omit_default": True, "wire_name": "message"})
    images: tuple[ImageInput, ...] | None = field(
        default=None, metadata={"wire_omit_default": True}
    )
    streaming_behavior: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "streamingBehavior"}
    )

    def to_rpc(self):
        data = super().to_rpc()
        if self.images is not None:
            data["images"] = [image.to_rpc() for image in self.images]
        return data

    @classmethod
    def decode_parameters(cls, data):
        if data.get("images") is not None:
            images = data["images"]
            if not isinstance(images, list) or any(
                not isinstance(image, dict) or image.get("type") != "image" for image in images
            ):
                raise ValueError("Expected native image inputs")
            data = {
                **data,
                "images": [
                    {key: value for key, value in image.items() if key != "type"}
                    for image in images
                ],
            }
        return data

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        from . import turn_phase as phases

        if session.initial_prompt_response:
            session.last_model_progress = session.now
            if response.success:
                session.initial_prompt_acknowledged = True
                session.prompt_accepted = True
                session.phase = phases.ModelWaitPhase()
            else:
                session.error_message = (
                    "Image prompt failed; backend diagnostics withheld."
                    if session.image_input_sent or session.inherited_image_sensitive
                    else str(response.error or "Prompt was rejected")
                )
                yield session.turn_state("failed", "prompt_rejected", 0, event_phase="shutdown")
                yield events.Error(text=session.error_message)
                session.finished = True
                return


@dataclass(frozen=True, kw_only=True)
class GetState(PiCommand, SessionSnapshot):
    response_payload = StateData

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if response.success:
            state = response.data or StateData()
            if not session.initial_session_observed:
                session.initial_session_id = state.session_id
                session.initial_session_file = state.session_file
                session.initial_session_observed = True
            session.model_name = state.model.display_name if state.model else None
            session.session_name = state.session_name
            session.active_session_file = state.session_file or session.active_session_file
            session.usage.size = state.model.context_window if state.model else None
            yield events.AgentInfo(
                model=state.model.display_name if state.model else None,
                thinking_level=state.thinking_level,
                session_name=state.session_name,
                session_file=session.active_session_file,
                context_used=session.usage.used,
                context_size=session.usage.size,
            )


@dataclass(frozen=True, kw_only=True)
class GetSessionStats(PiCommand, SessionSnapshot):
    response_payload = SessionStatsData

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if response.success:
            context = response.data.context_usage if response.data is not None else None
            if context is not None:
                if context.tokens is not None and context.tokens > 0:
                    session.usage.confirm(context.tokens)
                if not session.session_identity_uncertain:
                    session.usage.size = context.context_window or session.usage.size
            yield session.context_info()
            if session.persistent_session is None:
                session.finished = True


@dataclass(frozen=True, kw_only=True)
class GetAvailableModels(PiCommand):
    response_payload = ModelsData
    pass


@dataclass(frozen=True, kw_only=True)
class GetAvailableThinkingLevels(PiCommand):
    response_payload = ThinkingLevelsData
    pass


@dataclass(frozen=True, kw_only=True)
class Abort(PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class InterruptSteering(PiCommand):
    async def steer(self, session: TurnSession, forwarded: ForwardedInput) -> bool:
        return await session.inputs.interrupt(session, forwarded)

    input_ids: list[str] | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "inputIds"}
    )


@dataclass(frozen=True, kw_only=True)
class SetModel(PiCommand):
    provider: str | None = field(default=None, metadata={"wire_omit_default": True})
    model_id: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "modelId"}
    )

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        yield events.ModelChanged(
            id=response.id,
            ok=bool(response.success),
            error=response.error or "Model change failed",
        )


@dataclass(frozen=True, kw_only=True)
class SetThinkingLevel(PiCommand):
    level: str | None = field(default=None, metadata={"wire_omit_default": True})

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        yield events.ThinkingChanged(
            id=response.id,
            ok=bool(response.success),
            error=response.error or "Thinking level change failed",
        )


@dataclass(frozen=True, kw_only=True)
class Compact(PiCommand):
    response_payload = CompactionData
    custom_instructions: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "customInstructions"}
    )


@dataclass(frozen=True, kw_only=True)
class ExtensionUiResponse(PiCommand):
    cancelled: bool | None = field(default=None, metadata={"wire_omit_default": True})
    confirmed: bool | None = field(default=None, metadata={"wire_omit_default": True})
    value: str | None = field(default=None, metadata={"wire_omit_default": True})


@dataclass(frozen=True, kw_only=True)
class NewSession(MutatesSession, PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class SwitchSession(MutatesSession, PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class Fork(MutatesSession, PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class Clone(MutatesSession, PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class AgentCommsSummarizeCompaction(PiCommand):
    response_payload = SelectedSummaryData
    strict_response = True
    version: int
    operation_id: str = field(metadata={"wire_name": "operationId"})
    witness: NativeWitness
    selected: SelectedModel
    settings: SelectedSettings


@dataclass(frozen=True, kw_only=True)
class AgentCommsPrepareCompaction(PiCommand):
    response_payload = SelectedProbeData
    strict_response = True
    version: int = 1
    dry_run: bool = field(default=True, metadata={"wire_name": "dryRun"})
    witness: NativeWitness
    selected: SelectedModel
    settings: SelectedSettings


@dataclass(frozen=True, kw_only=True)
class AgentCommsCompactionSettings(PiCommand):
    response_payload = CompactionSettingsData
    strict_response = True
    version: int = 1
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})
    selected: SelectedModel
    context_tokens: int = field(metadata={"wire_name": "contextTokens"})
