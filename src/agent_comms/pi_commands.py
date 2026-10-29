"""Typed outbound Pi commands and command-owned response semantics."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import JsonValue

from . import agent_events as events
from . import turn_failure as failures
from .acp_failure import UnrecordedFailureSource
from .agent_backend import InputRequest, StreamingInputPolicy
from .child_process import BoundedRun
from .declared_family import DeclaredFamily, UnknownFamilyMember
from .field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    WireAbsentNone,
    WireName,
    WireOmitDefault,
    WireTransient,
)
from .image_input import ImageInput
from .native_failures import NativePiUnavailable
from .native_package import VerifiedNativePackage
from .native_pi import NativePiRpcLaunch
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
    ReportedText,
    RequestId,
    SessionStatsData,
    SessionSwitchData,
    StateData,
    ThinkingLevelsData,
    UnknownData,
)
from .turn_context import InputContributionCoordinates, SegmentManifest
from .turn_item import PiRequestKey

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import Response, ResponseOutcome
    from .pending_requests import PendingRequest
    from .launch_runtime import RestartEnvironment
    from .tracked_turn import TrackedTurnSession
    from .native_entries import NativeEvidenceScope
    from .native_input_proof import NativeContextProof
    from .pi_events import InputCommitted


class SessionSnapshot:
    """Reports the bound session identity before metadata projection."""

    def accepted(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        return response.data.require_payload().native_updates(session)

    @classmethod
    def invalidates_identity(cls, response: Response, session: TurnSession) -> bool:
        return response.success.conflicts_attestation(
            response.data, session.native.attestation,
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
        default_factory=lambda: RequestId(PiRequestKey.fresh())
    )

    def require_id(self) -> PiRequestKey:
        """Operations that correlate an input require the command's actual ID."""
        return self.id.require()

    @classmethod
    def invalidates_identity(cls, response: Response, session: TurnSession) -> bool:
        return False

    async def exchange(self, child, *, strict=False, max_bytes=None, on_event=lambda event: None):
        channel = child.reader
        pending = await child.send_request(self)
        while not pending.future.done():
            event = await channel.receive(strict=strict, max_bytes=max_bytes)
            if event is None:
                raise EOFError('Pi RPC ended before the requested response')
            event.correlate_or_deliver(channel, on_event)
        return pending.future.result()

    async def before_settlement(self, pending: PendingRequest[PiCommand]) -> None:
        """Ordinary metadata replies cannot start another native model run."""

    @classmethod
    def response_type(cls) -> type[PiCommand]:
        """The external command family; private request roles inherit its wire identity."""
        return cls

    def record_tag(self) -> dict[str, str]:
        return {self.family_discriminator: self.response_type().declared_name}

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
    def validate_response(
        record: Mapping[str, object], response: type[Response],
        outcome: ResponseOutcome, error: ReportedText | PiOmitted,
    ) -> None:
        outcome.validate_payload(error)

    def on_response(
        self, response: Response, session: TurnSession,
    ) -> Iterable[events.AgentEvent]:
        return response.success.apply_command(self, response, session)

    def accepted(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        return ()

    def rejected(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        failure = failures.BackendFailed(
            f"Native {self.response_type().declared_name} refused: {response.error.require()}"
        )
        session.output.record_failure(failure)
        return (session.output.error(failure.text, source=UnrecordedFailureSource()),)


class ExactResponse:
    """A selected operation's response contains exactly its declared success or error envelope."""

    @staticmethod
    def validate_response(
        record: Mapping[str, object], response: type[Response],
        outcome: ResponseOutcome, error: ReportedText | PiOmitted,
    ) -> None:
        expected = {key for _, key in FieldCodec.record_fields(response)} | {response.wire_tag}
        expected.difference_update(outcome.excluded_fields())
        if set(record) != expected:
            raise MalformedRecord("Unexpected selected response envelope")
        outcome.validate_payload(error)


@dataclass(frozen=True, kw_only=True)
class UnknownCommand(PiCommand):
    response_payload = UnknownData
    wire: dict[str, JsonValue]

    def to_rpc(self) -> dict[str, JsonValue]:
        return dict(self.wire)


@dataclass(frozen=True, kw_only=True)
class Prompt(PiCommand):
    input_id: Annotated[InputId, WireName("inputId")]
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

    async def input_committed(self, session: TrackedTurnSession, event: InputCommitted) -> None:
        session.prompt_state = session.prompt_state.start(event)
        await session.following.open(session)

    async def context_committed(
        self, session: TrackedTurnSession, context: NativeContextProof,
        source_reads: NativeEvidenceScope,
    ) -> None:
        await session.prompt_send_boundary.context_committed(context, source_reads)

    @classmethod
    def response_type(cls) -> type[PiCommand]:
        return Prompt

    async def before_settlement(self, pending: PendingRequest[PiCommand]) -> None:
        # Both original and following prompts can still be in async preflight.
        await pending.response()

    def on_response(
        self, response: Response, session: TurnSession,
    ) -> Iterable[events.AgentEvent]:
        session.watchdog.progress()
        return super().on_response(response, session)

    def accepted(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        session.prompt_state = session.prompt_state.acknowledge(response)
        session.watchdog.accepted()
        return (events.PromptAccepted(),)

    def rejected(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        return session.reject_prompt(response)


@dataclass(frozen=True, kw_only=True)
class FollowingPrompt(Prompt, declared_name=None):
    """A following input shares Pi's prompt wire shape, not the original input's effects."""

    input: Annotated[InputRequest, WireTransient()]

    async def input_committed(self, session: TrackedTurnSession, event: InputCommitted) -> None:
        """This accepted following input cannot restart the original prompt or inbox."""

    async def context_committed(
        self, session: TrackedTurnSession, context: NativeContextProof,
        source_reads: NativeEvidenceScope,
    ) -> None:
        await session.following.context_committed(self.input.input_id, self.message, context)

    def accepted(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        # PendingRequest already records this acknowledgement.
        return ()

    def rejected(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        return session.inputs.rejected(self.input_id, response, session)


class NativeQuery(PiCommand):
    """One correlated RPC transaction; command capabilities own its effects."""

    @property
    @abstractmethod
    def response_payload(self) -> type[PiResponseData]: ...


@dataclass(frozen=True, kw_only=True)
class GetState(SessionSnapshot, NativeQuery):
    response_payload = StateData

@dataclass(frozen=True, kw_only=True)
class GetSessionStats(SessionSnapshot, NativeQuery):
    response_payload = SessionStatsData

# Seconds for one metadata child to start and answer. An idle Pi boot answers in 6-7 s, so
# 10 s timed out `fork --model` on a loaded host (CI, a machine running tests).
CATALOG_TIMEOUT = 30


class CatalogQuery(NativeQuery):
    """Catalog discovery uses the shared typed query transaction."""

    @asynccontextmanager
    async def catalog(
        self, agent_bin: str, arguments: Sequence[str], *, package: VerifiedNativePackage,
        configuration: RestartEnvironment, worktree: Path, root: Path,
    ):
        """Acquire metadata commands without borrowing a saved or executing session."""
        from .native_child import PiChild
        from .pi_rpc import PiRpcChannel

        try:
            launch = await asyncio.to_thread(
                NativePiRpcLaunch.catalog,
                agent_bin,
                tuple(arguments),
                package=package,
                worktree=worktree,
                root=root,
                configuration=configuration,
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
        self, agent_bin: str, arguments: Sequence[str], root: Path,
        *, package: VerifiedNativePackage, configuration: RestartEnvironment, worktree: Path,
    ) -> PiResponseData:
        async with self.catalog(
            agent_bin, arguments, root=root, worktree=worktree,
            package=package, configuration=configuration,
        ) as child:
            response = await self.exchange(child)
            return response.require_data()


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
    def rejected(
        self, response: Response, session: TurnSession,
    ) -> Iterable[events.AgentEvent]:
        return (events.Error(text=response.error.require()),)

    input_ids: Annotated[
        list[InputId] | PiOmitted, WireOmitDefault(), WireName("inputIds")
    ] = PiOmitted()


class SettingCommand(CatalogQuery):
    response_payload = EmptyData

    @property
    @abstractmethod
    def result_type(self) -> type[events.SettingChangeResult]: ...

    async def configure(
        self, agent_bin: str, arguments: Sequence[str], *, package: VerifiedNativePackage, worktree: Path,
        root: Path, configuration: RestartEnvironment,
    ) -> StateData:
        """Let native settings own validation/defaults, then observe their result.

        The catalog session is in memory. Its normal SetModel/SetThinkingLevel
        semantics cannot change a running turn, a saved transcript or its budget.
        """
        catalog = self.catalog(
            agent_bin, arguments, package=package, worktree=worktree, root=root,
            configuration=configuration,
        )
        async with catalog as child:
            response = await self.exchange(child)
            response.success.require_success(response)
            state = await GetState().exchange(child)
            return state.require_data()

    def accepted(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        return (self.result_type(id=response.id, ok=True),)

    def rejected(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        return (self.result_type(id=response.id, ok=False, error=response.error.require()),)


@dataclass(frozen=True, kw_only=True)
class SetModel(SettingCommand):
    result_type = events.ModelChanged
    provider: Annotated[str | PiOmitted, WireOmitDefault()] = PiOmitted()
    model_id: Annotated[
        str | PiOmitted, WireOmitDefault(), WireName("modelId")
    ] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class SetThinkingLevel(SettingCommand):
    result_type = events.ThinkingChanged
    level: Annotated[str | PiOmitted, WireOmitDefault()] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class SetSteeringMode(PiCommand):
    """How many waiting steers Pi hands the model at one step: "all" or "one-at-a-time"."""

    mode: Annotated[str | PiOmitted, WireOmitDefault()] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class ClearQueue(PiCommand):
    """Remove exactly the selected queued native inputs."""

    response_payload = QueueClearedData
    input_ids: Annotated[tuple[InputId, ...], WireName("inputIds")]

    def accepted(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        session.inputs.cleared(self.input_ids, response, session)
        return ()

    def rejected(self, response: Response, session: TurnSession) -> Iterable[events.AgentEvent]:
        session.finish_request()
        return super().rejected(response, session)


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
