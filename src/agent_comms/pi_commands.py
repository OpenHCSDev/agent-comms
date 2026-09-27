"""Typed outbound Pi commands and command-owned response semantics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import agent_events as events
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec

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
        return FieldCodec.decode(cls, {"kind": kind, **data})

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
    images: Any = field(default=None, metadata={"wire_omit_default": True})
    streaming_behavior: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "streamingBehavior"}
    )

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        from . import turn_phase as phases

        if session.initial_prompt_response:
            session.last_model_progress = session.now
            if response.get("success"):
                session.initial_prompt_acknowledged = True
                session.prompt_accepted = True
                session.phase = phases.ModelWaitPhase()
            else:
                session.error_message = (
                    "Image prompt failed; backend diagnostics withheld."
                    if session.image_input_sent or session.inherited_image_sensitive
                    else str(response.get("error") or "Prompt was rejected")
                )
                yield session.turn_state("failed", "prompt_rejected", 0, event_phase="shutdown")
                yield events.Error(text=session.error_message)
                session.finished = True
                return


@dataclass(frozen=True, kw_only=True)
class GetState(PiCommand, SessionSnapshot):
    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if response.get("success"):
            session.command = response.get("command")
            session.data = response.get("data") or {}
            session.state_id = session.data.get("sessionId")
            session.state_file = session.data.get("sessionFile")
            if not session.initial_session_observed:
                session.initial_session_id = session.state_id
                session.initial_session_file = session.state_file
                session.initial_session_observed = True
            session.model = session.data.get("model") or {}
            session.provider = session.model.get("provider")
            session.model_id = session.model.get("id") or session.model.get("name")
            session.model_name = (
                f"{session.provider}/{session.model_id}"
                if session.provider and session.model_id
                else session.model_id or session.provider
            )
            session.session_name = session.data.get("sessionName")
            session.active_session_file = session.state_file or session.active_session_file
            session.usage.size = session.model.get("contextWindow")
            yield events.AgentInfo(
                model=session.model_name,
                thinking_level=session.data.get("thinkingLevel"),
                session_name=session.session_name,
                session_file=session.active_session_file,
                context_used=session.usage.used,
                context_size=session.usage.size,
            )


@dataclass(frozen=True, kw_only=True)
class GetSessionStats(PiCommand, SessionSnapshot):
    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if response.get("success"):
            session.command = response.get("command")
            session.data = response.get("data") or {}
            session.context = session.data.get("contextUsage") or {}
            session.tokens = (
                session.context.get("tokens") if isinstance(session.context, dict) else None
            )
            if type(session.tokens) is int and session.tokens > 0:
                session.usage.used = session.tokens
                session.usage.confirmed = session.tokens
            if isinstance(session.context, dict) and (not session.session_identity_uncertain):
                session.usage.size = session.context.get("contextWindow") or session.usage.size
            yield session.context_info()
            if session.persistent_session is None:
                session.finished = True
                return


@dataclass(frozen=True, kw_only=True)
class GetAvailableModels(PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class GetAvailableThinkingLevels(PiCommand):
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
            id=response.get("id"),
            ok=bool(response.get("success")),
            error=response.get("error", "Model change failed"),
        )


@dataclass(frozen=True, kw_only=True)
class SetThinkingLevel(PiCommand):
    level: str | None = field(default=None, metadata={"wire_omit_default": True})

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        yield events.ThinkingChanged(
            id=response.get("id"),
            ok=bool(response.get("success")),
            error=response.get("error", "Thinking level change failed"),
        )


@dataclass(frozen=True, kw_only=True)
class Compact(PiCommand):
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
