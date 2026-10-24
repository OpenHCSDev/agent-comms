"""Typed outbound Pi commands and command-owned response semantics."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar
from uuid import uuid4

from pydantic import JsonValue

from . import agent_events as events
from . import turn_phase as phases
from .agent_backend import StreamingInputPolicy
from .declared_family import DeclaredFamily, UnknownFamilyMember
from .field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    WireAbsentNone,
    WireName,
    WireOmitDefault,
)
from .image_input import ImageInput
from .native_failures import NativePiUnavailable
from .native_package import VerifiedNativePackage
from .native_session_reopen import NativeSessionIdentity
from .native_tool_call import OwnerToolConnection
from .native_turn_context import NativeContextData
from .pi_payloads import (
    CompactionData,
    EmptyData,
    ModelsData,
    PiOmitted,
    PiRequestId,
    PiResponseData,
    QueueClearedData,
    RequestId,
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
            response.succeeded
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
    # The answering child appends to its own session file (a retained child stays current).
    writes_session: ClassVar[bool] = False
    id: Annotated[PiRequestId, WireAbsentNone()] = field(
        default_factory=lambda: RequestId(uuid4().hex)
    )

    def require_id(self) -> str:
        """Operations that correlate an input require the command's actual ID."""
        return self.id.require()

    @classmethod
    def invalidates_identity(cls, response: Response, session: TurnSession) -> bool:
        return False

    @asynccontextmanager
    async def pending_response(self, child):
        channel = child.reader
        future = child.send(self)
        try:
            await child.drain()
            yield future
        finally:
            self.id.release_from(channel.pending, type(self))
            future.cancel()

    async def exchange(self, child, *, strict=False, max_bytes=None, on_event=lambda event: None):
        channel = child.reader
        async with self.pending_response(child) as future:
            while not future.done():
                event = await channel.receive(strict=strict, max_bytes=max_bytes)
                if event is None:
                    raise EOFError('Pi RPC ended before the requested response')
                event.correlate_or_deliver(channel, on_event)
            return future.result()

    def to_rpc(self) -> dict[str, JsonValue]:
        data = FieldCodec.encode(self)
        data["type"] = data.pop("kind")
        return data

    @classmethod
    def from_wire(cls, value: dict[str, JsonValue]) -> PiCommand:
        data = dict(value)
        kind = data.pop("type", None)
        try:
            cls.member_named(kind)
        except UnknownFamilyMember:
            return UnknownCommand(id=FieldCodec.field_plan(cls, "id").read(data), wire=value)
        return FieldCodec.decode(cls, {"kind": kind, **data})

    @classmethod
    def response_owner(cls, name: object) -> type[PiCommand]:
        try:
            return cls.member_named(FieldCodec.decode(str, name))
        except UnknownFamilyMember:
            return UnknownCommand

    @classmethod
    def decode_response_data(cls, value: object) -> object:
        return cls.response_payload.decode_external(value, {})

    @staticmethod
    def validate_response(record: Mapping[str, object], response: type[Response]) -> None:
        pass

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        return ()


class ExactResponse:
    """A selected operation's response contains exactly its declared success or error envelope."""

    @staticmethod
    def validate_response(record: Mapping[str, object], response: type[Response]) -> None:
        outcome = response.outcome(record)
        expected = {key for _, key in FieldCodec.record_fields(response)} | {response.wire_tag}
        expected.difference_update(outcome.excluded_fields())
        if set(record) != expected:
            raise MalformedRecord("Unexpected selected response envelope")
        outcome.validate_payload(record)


@dataclass(frozen=True, kw_only=True)
class UnknownCommand(PiCommand):
    response_payload = UnknownData
    wire: dict[str, JsonValue]

    def to_rpc(self) -> dict[str, JsonValue]:
        return dict(self.wire)


@dataclass(frozen=True, kw_only=True)
class Prompt(PiCommand):
    input_id: Annotated[
        InputId | PiOmitted, WireOmitDefault(), WireName("inputId")
    ] = PiOmitted()
    message: Annotated[str, WireOmitDefault()] = None
    # Pi never receives an empty image list: no images and omission are one value.
    images: Annotated[tuple[ImageInput, ...], WireOmitDefault()] = ()
    context_contributions: Annotated[
        tuple[InputContributionCoordinates, ...],
        WireOmitDefault(),
        WireName("contextContributions"),
    ] = ()
    owner_tools: Annotated[OwnerToolConnection, WireName("ownerTools")]
    streaming_behavior: Annotated[
        type[StreamingInputPolicy] | None,
        WireOmitDefault(),
        WireName("streamingBehavior"),
    ] = None

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        if response.id == session.command.id:
            session.watchdog.progress()
            if response.succeeded:
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
        if response.succeeded:
            state = response.data.require_payload()
            session.native.attestation = session.native.attestation.observe(state)
            session.active_session_file = state.session_file.text_or(session.active_session_file)
            state.model.context_window.observe_size(session.usage)
            yield state.agent_info(session)


@dataclass(frozen=True, kw_only=True)
class GetSessionStats(SessionSnapshot, NativeQuery):
    response_payload = SessionStatsData

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        if response.succeeded:
            for context in response.data.require_payload().context_usage.present():
                context.token_total.confirm(session.usage)
                if not session.native.attestation.uncertain:
                    context.context_window.update_size(session.usage)
            yield session.context_info()


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
        from .native_child import PiChild
        from .native_pi import NativePiRpcLaunch
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
            async with BoundedRun.session(
                launch.argv, cwd=launch.cwd, env=launch.env, timeout=CATALOG_TIMEOUT
            ) as child:
                yield PiChild(child, PiRpcChannel(child.stdout))
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
        async with self.catalog(agent_bin, arguments, root=root, package=package) as child:
            response = await self.exchange(child)
            if response.succeeded:
                return response.data.require_payload()
            raise NativePiUnavailable(response.error.text_or("Native catalog query was refused"))


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
        if response.refused:
            yield events.Error(text=response.error.text_or("Send now was refused"))

    input_ids: Annotated[
        list[str] | PiOmitted, WireOmitDefault(), WireName("inputIds")
    ] = PiOmitted()


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
        catalog = self.catalog(agent_bin, arguments, package=package, worktree=worktree, root=root)
        async with catalog as child:
            response = await self.exchange(child)
            if not response.succeeded:
                raise NativePiUnavailable(response.error.text_or(self.error_message))
            state = await GetState().exchange(child)
            if not state.succeeded:
                raise NativePiUnavailable(
                    state.error.text_or("Native configuration was not reported")
                )
            return state.data.require_payload()

    @classmethod
    def on_response(
        cls, response: Response, session: TurnSession
    ) -> Iterable[events.AgentEvent]:
        yield cls.result_type(
            id=response.id,
            ok=response.succeeded,
            error=response.error.text_or(cls.error_message),
        )


@dataclass(frozen=True, kw_only=True)
class SetModel(SettingCommand):
    result_type = events.ModelChanged
    error_message = "Model change failed"
    provider: Annotated[str | PiOmitted, WireOmitDefault()] = PiOmitted()
    model_id: Annotated[
        str | PiOmitted, WireOmitDefault(), WireName("modelId")
    ] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class SetThinkingLevel(SettingCommand):
    result_type = events.ThinkingChanged
    error_message = "Thinking level change failed"
    level: Annotated[str | PiOmitted, WireOmitDefault()] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class SetSteeringMode(PiCommand):
    """How many waiting steers Pi hands the model at one step: "all" or "one-at-a-time"."""

    mode: Annotated[str | PiOmitted, WireOmitDefault()] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class ClearQueue(PiCommand):
    """Remove exactly the selected queued native inputs."""

    response_payload = QueueClearedData
    input_ids: Annotated[tuple[str, ...], WireName("inputIds")]


@dataclass(frozen=True, kw_only=True)
class Compact(NativeQuery):
    """Pi's own compaction engine, run now; Pi adds Core's task brief to the instructions."""

    response_payload = CompactionData
    writes_session = True
    custom_instructions: Annotated[
        str | PiOmitted, WireOmitDefault(), WireName("customInstructions")
    ] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class ExtensionUiResponse(PiCommand):
    cancelled: Annotated[bool | PiOmitted, WireOmitDefault()] = PiOmitted()
    confirmed: Annotated[bool | PiOmitted, WireOmitDefault()] = PiOmitted()
    value: Annotated[str | PiOmitted, WireOmitDefault()] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class NewSession(MutatesSession, PiCommand):
    pass


@dataclass(frozen=True, kw_only=True)
class SwitchSession(ExactResponse, MutatesSession, NativeQuery):
    response_payload = SessionSwitchData
    session_path: Annotated[str, WireName("sessionPath")]


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
