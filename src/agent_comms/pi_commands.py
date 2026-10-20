"""Typed outbound Pi commands and command-owned response semantics."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from uuid import uuid4

from . import agent_events as events
from . import turn_phase as phases
from .declared_family import DeclaredFamily, UnknownFamilyMember
from .field_codec import FieldCodec
from .image_inputs import ImageInput
from .native_package import VerifiedNativePackage
from .native_session_reopen import NativeSessionIdentity
from .native_tool_call import OwnerToolConnection
from .native_turn_context import NativeContextData
from .pi_payloads import (
    CompactionData,
    EmptyData,
    ModelsData,
    PiResponseData,
    QueueClearedData,
    SessionStatsData,
    SessionSwitchData,
    StateData,
    ThinkingLevelsData,
    UnknownData,
)
from .turn_context import InputContributionCoordinates, SegmentManifest

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import Response


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


@dataclass(frozen=True, kw_only=True)
class PiCommand(DeclaredFamily):
    response_payload: ClassVar[type[PiResponseData]] = EmptyData
    strict_response: ClassVar[bool] = False
    # The answering child appends to its own session file (a retained child stays current).
    writes_session: ClassVar[bool] = False
    id: str | None = field(
        default_factory=lambda: uuid4().hex, metadata={"wire_absent_none": True}
    )

    @classmethod
    def invalidates_identity(cls, response: Response, session: TurnSession) -> bool:
        return False

    @asynccontextmanager
    async def pending_response(self, channel, writer):
        future = channel.track(self)
        try:
            writer.write(channel.command_bytes(self))
            await writer.drain()
            yield future
        finally:
            channel.pending.discard(type(self), self.id)
            future.cancel()

    async def exchange(self, channel, writer, *, strict=False, max_bytes=None, on_event=lambda event: None):
        from .pi_events import Response
        async with self.pending_response(channel, writer) as future:
            while not future.done():
                event = await channel.receive(strict=strict, max_bytes=max_bytes)
                if event is None:
                    raise EOFError('Pi RPC ended before the requested response')
                if isinstance(event, Response):
                    channel.correlate(event)
                else:
                    on_event(event)
            return future.result()

    def to_rpc(self) -> dict[str, Any]:
        data = FieldCodec.encode(self)
        data["type"] = data.pop("kind")
        return data

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> PiCommand:
        data = dict(value)
        kind = data.pop("type", None)
        try:
            cls.member_named(kind)
        except UnknownFamilyMember:
            return UnknownCommand(id=FieldCodec.field_plan(cls, "id").read(data), wire=value)
        return FieldCodec.decode(cls, {"kind": kind, **data})

    @classmethod
    def response_owner(cls, name: str) -> type[PiCommand]:
        try:
            return cls.member_named(name)
        except UnknownFamilyMember:
            return UnknownCommand

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        return ()


@dataclass(frozen=True, kw_only=True)
class UnknownCommand(PiCommand):
    response_payload = UnknownData
    wire: dict[str, Any]

    def to_rpc(self) -> dict[str, Any]:
        return dict(self.wire)


@dataclass(frozen=True, kw_only=True)
class Prompt(PiCommand):
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
    owner_tools: OwnerToolConnection = field(metadata={"wire_name": "ownerTools"})
    streaming_behavior: Literal[None, "steer", "followUp"] = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "streamingBehavior"}
    )

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        if response.id == session.command.id:
            session.watchdog.progress()
            if response.success:
                session.prompt_state = session.prompt_state.acknowledge(response)
                session.watchdog.prompt_accepted = True
                session.watchdog.phase = phases.ModelWaitPhase()
                yield events.PromptAccepted()
            else:
                yield from session.reject_prompt(response)
                return
        else:
            session.inputs.acknowledge(response)


class NativeQuery(PiCommand):
    """One correlated RPC transaction; command capabilities own its effects."""

    @property
    @abstractmethod
    def response_payload(self) -> type[PiResponseData]: ...


@dataclass(frozen=True, kw_only=True)
class GetState(SessionSnapshot, NativeQuery):
    response_payload = StateData

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        if response.success:
            state = response.data.require_payload()
            session.native.attestation = session.native.attestation.observe(state)
            session.model_name = state.model.display_name
            session.active_session_file = state.session_file or session.active_session_file
            state.model.context_window.observe_size(session.usage)
            yield state.agent_info(session)


@dataclass(frozen=True, kw_only=True)
class GetSessionStats(SessionSnapshot, NativeQuery):
    response_payload = SessionStatsData

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        if response.success:
            context = response.data.require_payload().context_usage
            if context is not None:
                context.token_total.confirm(session.usage)
                if not session.native.attestation.uncertain:
                    context.context_window.update_size(session.usage)
            yield session.context_info()
            if session.persistent_session is None:
                session.finished = True


CATALOG_TIMEOUT = 10  # seconds for one metadata child to start and answer


class CatalogQuery(NativeQuery):
    """Catalog discovery uses the shared typed query transaction."""

    @asynccontextmanager
    async def catalog(
        self, agent_bin: str, arguments: Sequence[str], *, package: VerifiedNativePackage,
        worktree: Path | None = None, root: Path | str | None = None,
    ):
        """Acquire metadata commands without borrowing a saved or executing session."""
        from .child_process import BoundedRun
        from .native_pi import NativePiRpcLaunch, NativePiUnavailable
        from .pi_rpc import PiRpcChannel

        try:
            launch = await asyncio.to_thread(
                NativePiRpcLaunch.catalog,
                agent_bin,
                tuple(arguments),
                package=package,
                worktree=Path.cwd() if worktree is None else worktree,
                root=root,
            )
            from .native_child import PiChild

            async with BoundedRun.session(
                launch.argv, cwd=launch.cwd,
                env=PiChild.native_environment(launch.record, launch.cwd, launch.env),
                timeout=CATALOG_TIMEOUT, runtime=launch.record.runtime,
            ) as child:
                yield PiRpcChannel(child.stdout), child.stdin
        except TimeoutError as error:
            raise NativePiUnavailable(
                f"Native catalog discovery timed out: no catalog reply from {agent_bin!r} "
                f"within {CATALOG_TIMEOUT} s"
            ) from error
        except (EOFError, ValueError, OSError) as error:
            raise NativePiUnavailable(f"Native catalog discovery failed: {error!r}") from error

    async def discover(
        self, agent_bin: str, arguments: Sequence[str], root: Path | str | None = None,
        *, package: VerifiedNativePackage,
    ) -> PiResponseData:
        from .native_pi import NativePiUnavailable

        async with self.catalog(agent_bin, arguments, root=root, package=package) as (channel, writer):
            response = await self.exchange(channel, writer)
            if response.success is True:
                return response.data.require_payload()
            raise NativePiUnavailable(response.error or "Native catalog query was refused")


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
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        if response.success is False:
            yield events.Error(text=str(response.error or "Send now was refused"))

    input_ids: list[str] | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "inputIds"}
    )


class SettingCommand(CatalogQuery):
    response_payload = EmptyData

    @property
    @abstractmethod
    def result_type(self) -> type[events.SettingChangeResult]: ...

    error_message: ClassVar[str]

    async def configure(
        self, agent_bin: str, arguments: Sequence[str], *, package: VerifiedNativePackage, worktree: Path,
        root: Path | str | None = None,
    ) -> StateData:
        """Let native settings own validation/defaults, then observe their result.

        The catalog session is in memory. Its normal SetModel/SetThinkingLevel
        semantics cannot change a running turn, a saved transcript or its budget.
        """
        from .native_pi import NativePiUnavailable

        catalog = self.catalog(agent_bin, arguments, package=package, worktree=worktree, root=root)
        async with catalog as (channel, writer):
            response = await self.exchange(channel, writer)
            if response.success is not True:
                raise NativePiUnavailable(response.error or self.error_message)
            state = await GetState().exchange(channel, writer)
            if state.success is not True:
                raise NativePiUnavailable(state.error or "Native configuration was not reported")
            return state.data.require_payload()

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        yield cls.result_type(
            id=response.id,
            ok=bool(response.success),
            error=response.error or cls.error_message,
        )


@dataclass(frozen=True, kw_only=True)
class SetModel(SettingCommand):
    @classmethod
    def from_name(cls, model: str | None):
        if model is None:
            return cls()
        provider, model_id = model.split('/', 1)
        return cls(provider=provider, model_id=model_id)

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
class SetSteeringMode(PiCommand):
    """How many waiting steers Pi hands the model at one step: "all" or "one-at-a-time"."""

    mode: str | None = field(default=None, metadata={"wire_omit_default": True})


@dataclass(frozen=True, kw_only=True)
class ClearQueue(PiCommand):
    """Remove exactly the selected queued native inputs."""

    response_payload = QueueClearedData
    input_ids: tuple[str, ...] = field(metadata={"wire_name": "inputIds"})


@dataclass(frozen=True, kw_only=True)
class Compact(NativeQuery):
    """Pi's own compaction engine, run now; Pi adds Core's task brief to the instructions."""

    response_payload = CompactionData
    writes_session = True
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
class SwitchSession(MutatesSession, NativeQuery):
    response_payload = SessionSwitchData
    strict_response = True
    session_path: str = field(metadata={"wire_name": "sessionPath"})


@dataclass(frozen=True, kw_only=True)
class Fork(MutatesSession, PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class Clone(MutatesSession, PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class AgentCommsInspectContext(NativeQuery):
    response_payload = NativeContextData


@dataclass(frozen=True, kw_only=True)
class AgentCommsInspectContextSegment(NativeQuery):
    response_payload = NativeContextData
    identity: NativeSessionIdentity
    entries: tuple[str, ...]
    expected: SegmentManifest
    parts: tuple[SegmentManifest, ...]

    @classmethod
    def for_manifest(cls, expected: SegmentManifest):
        return cls(identity=expected.sources.native_identity(), entries=expected.sources.journal_entries(),
                   expected=expected, parts=expected.requested_parts())
