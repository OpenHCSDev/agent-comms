"""Typed outbound Pi commands and command-owned response semantics."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from .turn_phase import ShutdownPhase

from .native_turn_context import NativeContextData
from .pi_vocabulary import ThinkingLevel, CompactionReason, ThresholdCompactionReason
from .message_reference import MessageReference
from . import agent_events as events
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .image_inputs import ImageInput
from .turn_context import InputContributionCoordinates
from .owner_compaction_prepare import NativeWitness, PrepareCompactionHelper
from .owner_compaction_settings import PiCompactionSettings
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
    CompactionPreparationData,
    CompactionSettingsData,
    SelectedModel,
    SelectedSummaryData,
)

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import Response
    from .pi_rpc import PiRpcChannel
    from .turn_inputs import ForwardedInput


class SessionSnapshot:
    """Reports the bound session identity before metadata projection."""

    @classmethod
    def invalidates_identity(cls, response: Response, session: TurnSession) -> bool:
        return bool(
            response.success
            and response.data.conflicts_attestation(session.native.attestation)
        ) or super().invalidates_identity(response, session)


class MutatesSession:
    """A command may change the native session identity before its response."""

    @classmethod
    def invalidates_identity(cls, response: Response, session: TurnSession) -> bool:
        return True

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

    @classmethod
    def invalidates_identity(cls, response: Response, session: TurnSession) -> bool:
        return False

    async def steer(self, session: TurnSession, forwarded: ForwardedInput) -> bool:
        session.stdin.write(session.native.reader.encode(self))
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
    context_contributions: tuple[InputContributionCoordinates, ...] = field(
        default=(), metadata={"wire_omit_default": True, "wire_name": "contextContributions"}
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

        if response.id == session.prompt_id:
            session.watchdog.progress()
            if response.success:
                session.admission = session.admission.acknowledge(response)
                session.watchdog.prompt_accepted = True
                session.watchdog.phase = phases.ModelWaitPhase()
            else:
                error = session.output.error(str(response.error or "Prompt was rejected"))
                yield session.watchdog.state(
                    "failed", "prompt_rejected", 0, phase=ShutdownPhase()
                )
                yield error
                session.finished = True
                return
        else:
            session.inputs.acknowledge(response)


@dataclass(frozen=True, kw_only=True)
class GetState(SessionSnapshot, PiCommand):
    response_payload = StateData

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if response.success:
            state = response.data.require_payload()
            session.native.attestation = session.native.attestation.observe(state)
            session.model_name = state.model.display_name
            session.session_name = state.session_name
            session.active_session_file = state.session_file or session.active_session_file
            session.usage.size = state.model.context_window
            yield events.AgentInfo(
                model=state.model.display_name,
                thinking_level=ThinkingLevel.optional_name(state.thinking_level),
                session_name=state.session_name,
                session_file=session.active_session_file,
                context_used=session.usage.used,
                context_size=session.usage.size,
            )


@dataclass(frozen=True, kw_only=True)
class GetSessionStats(SessionSnapshot, PiCommand):
    response_payload = SessionStatsData

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if response.success:
            context = response.data.require_payload().context_usage
            if context is not None:
                if context.tokens is not None and context.tokens > 0:
                    session.usage.confirm(context.tokens)
                if not session.native.attestation.uncertain:
                    session.usage.size = context.context_window or session.usage.size
            yield session.context_info()
            if session.persistent_session is None:
                session.finished = True


class NativeQuery(PiCommand):
    """One read-only native query owns launch, correlation and child retirement."""

    @property
    @abstractmethod
    def response_payload(self) -> type[PiResponseData]: ...

    @asynccontextmanager
    async def pending_response(self, channel, writer):
        command = replace(self, id=self.id or uuid4().hex)
        future = channel.track(command)
        try:
            writer.write(channel.command_bytes(command))
            await writer.drain()
            yield future
        finally:
            channel.pending.discard(type(command), command.id)
            future.cancel()

    async def exchange(
        self, channel: PiRpcChannel, writer: asyncio.StreamWriter, *,
        strict: bool = False, max_bytes: int | None = None,
    ) -> Response:
        from .pi_events import Response
        async with self.pending_response(channel,writer) as future:
            while not future.done():
                event = await channel.receive(strict=strict, max_bytes=max_bytes)
                if event is None:
                    raise EOFError("Pi RPC ended before the requested response")
                if isinstance(event,Response):
                    channel.correlate(event)
            return future.result()


class CatalogQuery(NativeQuery):
    """Catalog discovery uses the shared typed query transaction."""

    async def discover(self, agent_bin: str, arguments: Sequence[str]) -> PiResponseData:
        from .child_process import BoundedRun
        from .native_pi import NativePiRpcLaunch, NativePiUnavailable
        from .pi_rpc import PiRpcChannel

        try:
            launch = await asyncio.to_thread(
                NativePiRpcLaunch.managed,
                agent_bin,
                (
                    *arguments,
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--no-session",
                ),
                worktree=Path.cwd(),
            )
            async with BoundedRun.session(
                launch.argv, cwd=launch.cwd, env=launch.env, timeout=10
            ) as child:
                stderr = asyncio.create_task(child.discard_stderr())
                try:
                    response = await self.exchange(PiRpcChannel(child.stdout), child.stdin)
                    if response.success is True:
                        return response.data.require_payload()
                finally:
                    stderr.cancel()
                    await asyncio.gather(stderr, return_exceptions=True)
        except (TimeoutError, EOFError, ValueError, OSError, NativePiUnavailable):
            pass
        return self.response_payload()


@dataclass(frozen=True, kw_only=True)
class GetAvailableModels(CatalogQuery):
    response_payload = ModelsData


@dataclass(frozen=True, kw_only=True)
class GetAvailableThinkingLevels(CatalogQuery):
    response_payload = ThinkingLevelsData


@dataclass(frozen=True, kw_only=True)
class Abort(PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class InterruptSteering(PiCommand):
    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        if response.success is False:
            yield events.Error(text=str(response.error or "Send now was refused"))

    async def steer(self, session: TurnSession, forwarded: ForwardedInput) -> bool:
        return await session.inputs.interrupt(session, forwarded)

    input_ids: list[str] | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "inputIds"}
    )


class SettingCommand(PiCommand):
    @property
    @abstractmethod
    def result_type(self) -> type[events.SettingChangeResult]: ...

    error_message: ClassVar[str]

    @classmethod
    async def on_response(
        cls, response: Response, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        yield cls.result_type(
            id=response.id,
            ok=bool(response.success),
            error=response.error or cls.error_message,
        )


@dataclass(frozen=True, kw_only=True)
class SetModel(SettingCommand):
    result_type = events.ModelChanged
    error_message = "Model change failed"
    provider: str | None = field(default=None, metadata={"wire_omit_default": True})
    model_id: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "modelId"}
    )


@dataclass(frozen=True, kw_only=True)
class SetThinkingLevel(SettingCommand):
    result_type = events.ThinkingChanged
    error_message = "Thinking level change failed"
    level: str | None = field(default=None, metadata={"wire_omit_default": True})


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
    settings: PiCompactionSettings
    retained_text: str = field(metadata={"wire_name": "retainedText"})

    custom_instructions: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "customInstructions"}
    )


@dataclass(frozen=True, kw_only=True)
class AgentCommsCompactionSettings(NativeQuery):
    response_payload = CompactionSettingsData
    strict_response = True
    observation_timeout_seconds: ClassVar[float] = 5
    default_observation_timeout_seconds: ClassVar[float] = 3
    version: int = 2
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})
    selected: SelectedModel
    purpose: type[CompactionReason] = ThresholdCompactionReason
    boundary: tuple[MessageReference, ...] = ()


@dataclass(frozen=True, kw_only=True)
class AgentCommsPrepareCompaction(NativeQuery):
    response_payload = CompactionPreparationData
    strict_response = True
    # Preparing the whole history retains the original preparation operation's
    # budget. It is not the inexpensive selected-settings observation.
    observation_timeout_seconds: ClassVar[float] = PrepareCompactionHelper.timeout_seconds
    version: int = 1
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})
    selected: SelectedModel
    settings: PiCompactionSettings
    retained_text: str = field(default="", metadata={"wire_name": "retainedText"})


@dataclass(frozen=True, kw_only=True)
class AgentCommsInspectContext(NativeQuery):
    response_payload = NativeContextData
