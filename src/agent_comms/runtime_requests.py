"""Declaration-owned commands for the existing local owner socket protocol.

Only the socket boundary translates the external ``action`` tag. Request
parameters use A2; membership and dispatch use A1; execution uses A5. These
commands do not own coordination, backend or identity lifecycles.
"""

from __future__ import annotations

import asyncio
import json
from abc import abstractmethod
from functools import partial
from .coordinator import Coordination
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from acp.schema import PromptResponse, SetSessionConfigOptionResponse
from pydantic import BaseModel, JsonValue

from .command import Command
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, WireValue
from .private_bus_checkpoint import ContextManifestReference
from .goal_actions import GoalAction
from .goal_history import RecordedGoalRevision
from .goals import Goal
from .owner_protocol import ClientLine, OwnerAnnouncement, OwnerLine, OwnerReady, OwnerResult
from .thread_presentation import LiveThreadOwnerBinding
from .turn_context import ContextManifest, ContextSourceText, PreviewProvenance, Provenance, RecordedContextTurn, SegmentManifest, TurnContext
from .thread_identity import ThreadIncarnation
from .tool_result import DeclaredReply
from .working_memory_labels import ClassifierVersion, ModelLabel
from .working_memory_questions import SpanAnswer

if TYPE_CHECKING:
    from .acp_extension import GoalSnapshotReply
    from .agent_backend import CompactionOutcome
    from .input_disposition import DeliveryOverview
    from .runtime import RuntimeServer, SocketClient


@dataclass(frozen=True)
class RuntimeRequestContext:
    server: RuntimeServer
    reader: asyncio.StreamReader
    client: SocketClient
    session_id: str
    name: str

    async def send(self, line: OwnerLine) -> None:
        await self.client.send(line)


@dataclass(frozen=True, kw_only=True)
class RuntimeRequest(DeclaredFamily, Command, affix="RuntimeRequest"):
    thread: str

    @classmethod
    def from_wire(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise ValueError("Expected a runtime request object.")
        if "kind" in payload:
            raise ValueError("Unknown runtime request field: kind")
        values = dict(payload)
        values["kind"] = values.pop("action", None)
        return cls.from_payload(values)

    def to_wire(self) -> dict[str, object]:
        payload = FieldCodec.encode(self)
        return {"action": payload.pop("kind"), **payload}

    @classmethod
    def proxy_payload(
        cls, thread: str, controller_token: str | None, parameters: dict[str, object]
    ) -> dict[str, object]:
        # The owner decodes once; proxies preserve the owner's error envelope.
        return {"action": FieldCodec.encode(cls), "thread": thread,
                **FieldCodec.encode(parameters)}

    def require_owner(self, entry):
        owner = entry.thread
        if not owner.process_identity.runs_here() or not entry.status.running:
            raise RuntimeError("This process no longer owns the thread.")
        return owner

    async def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient
    ) -> RuntimeRequestContext:
        entry = (await Coordination.run_worker(partial(
            server.agent._comms.registry.entry, self.thread,
        ))).entry
        owner = self.require_owner(entry)
        session_id = server.agent.sessions.require_owned_session(entry)
        return RuntimeRequestContext(server, reader, client, session_id, owner.name)


class ResultRuntimeRequest(RuntimeRequest):
    async def apply(self, ctx: RuntimeRequestContext) -> None:
        reply = await self.result(ctx)
        await ctx.send(OwnerResult(result=FieldCodec.encode(reply)))

    @abstractmethod
    async def result(self, ctx: RuntimeRequestContext) -> DeclaredReply: ...


@dataclass(frozen=True)
class ProjectReply(DeclaredReply):
    worktree: str


@dataclass(frozen=True)
class CancelReply(DeclaredReply):
    pass


@dataclass(frozen=True)
class GoalReply(DeclaredReply):
    goal: Goal


@dataclass(frozen=True)
class GoalHistoryReply(DeclaredReply):
    history: tuple[RecordedGoalRevision, ...]


@dataclass(frozen=True)
class AcpResponseReply(DeclaredReply, WireValue):
    """An ACP schema response, sent exactly as the schema writes it."""

    response_type: ClassVar[type[BaseModel]]
    response: BaseModel

    def to_wire(self) -> dict[str, JsonValue]:
        return self.response.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_wire(cls, data: dict[str, JsonValue]) -> Self:
        return cls(cls.response_type.model_validate(data))


class PromptReply(AcpResponseReply):
    response_type = PromptResponse


class ConfigOptionReply(AcpResponseReply):
    response_type = SetSessionConfigOptionResponse


@dataclass(frozen=True)
class AnnotationsReply(DeclaredReply, WireValue):
    """A segment's stored annotations, sent as their list."""

    labels: tuple[ModelLabel, ...]

    def to_wire(self) -> tuple[ModelLabel, ...]:
        return self.labels

    @classmethod
    def from_wire(cls, data: list[JsonValue]) -> Self:
        return cls(FieldCodec.decode(tuple[ModelLabel, ...], data))


@dataclass(frozen=True, kw_only=True)
class ProjectRuntimeRequest(ResultRuntimeRequest):
    """Current project observation through the original native launch binding.

    This command grants no input or tool execution. A retained native process
    must still observe its original registry lease before each project check.
    """
    binding: LiveThreadOwnerBinding

    @classmethod
    def for_native(cls, snapshot, thread):
        process = thread.require_process()
        owner = snapshot.owner_identity(thread.name)
        snapshot.require_owner_process(owner, process)
        return cls(thread=thread.name, binding=LiveThreadOwnerBinding(owner, process))

    def environment(self, root):
        from .runtime import socket_path

        return {
            "AGENT_COMMS_PROJECT_SOCKET": str(socket_path(root, self.binding.process.pid)),
            "AGENT_COMMS_PROJECT_REQUEST": json.dumps(self.to_wire()),
        }

    def require_original(self, entry):
        if not entry.matches_name(self.thread):
            raise ValueError("Project request names another original thread")
        return entry.require_owner_process(self.binding.owner, self.binding.process)

    def require_owner(self, entry):
        self.require_original(entry)
        return super().require_owner(entry)

    async def result(self, ctx):
        entry = (await Coordination.run_worker(partial(
            ctx.server.agent._comms.registry.entry, ctx.name,
        ))).entry
        current = self.require_original(entry)
        return ProjectReply(current.worktree)


@dataclass(frozen=True, kw_only=True)
class SubscribeRuntimeRequest(RuntimeRequest):
    async def apply(self, ctx: RuntimeRequestContext) -> None:
        agent = ctx.server.agent
        await ctx.send(OwnerAnnouncement())
        ctx.server.clients.setdefault(ctx.session_id, set()).add(ctx.client)
        await agent.emit_session_identity(ctx.session_id, ctx.name, client=ctx.client)
        await agent.sessions.transcript.subscribe(ctx.session_id, client=ctx.client)
        await agent.turns.publish_turn_state(ctx.session_id, client=ctx.client)
        await agent.inputs.emit_input_delivery_changed(ctx.session_id, client=ctx.client)
        metadata = await agent.sessions.metadata(ctx.name)
        await ctx.send(
            OwnerReady(
                ready=metadata,
                controller_token=ctx.client.token,
            )
        )
        await agent.sessions.config.observe_configuration(ctx.session_id, ctx.client)
        while line := await ctx.reader.readline():
            ClientLine.read(line).answer(ctx.client)


@dataclass(frozen=True, kw_only=True)
class PromptRuntimeRequest(ResultRuntimeRequest):
    prompt: list[dict[str, object]]
    meta: dict[str, object] | None = None
    controller_token: str | None = field(default=None, metadata={"wire_name": "controllerToken"})

    @classmethod
    def proxy_payload(
        cls, thread: str, controller_token: str | None, parameters: dict[str, object]
    ) -> dict[str, object]:
        return super().proxy_payload(
            thread, controller_token, {"controllerToken": controller_token, **parameters}
        )

    async def result(self, ctx: RuntimeRequestContext) -> PromptReply:
        controller = next(
            (
                subscriber
                for subscriber in ctx.server.clients.get(ctx.session_id, ())
                if subscriber.token == self.controller_token and not subscriber.writer.is_closing()
            ),
            None,
        )
        token = ctx.server.controller.set(controller)
        try:
            result = await ctx.server.agent.prompt(
                ctx.session_id, self.prompt, field_meta=self.meta or {}
            )
        finally:
            ctx.server.controller.reset(token)
        return PromptReply(result)


@dataclass(frozen=True, kw_only=True)
class CancelRuntimeRequest(ResultRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> CancelReply:
        await ctx.server.agent.cancel(ctx.session_id)
        return CancelReply()


@dataclass(frozen=True, kw_only=True)
class SetConfigOptionRuntimeRequest(ResultRuntimeRequest):
    config_id: str
    value: str | bool

    async def result(self, ctx: RuntimeRequestContext) -> ConfigOptionReply:
        result = await ctx.server.agent.set_config_option(
            self.config_id, ctx.session_id, self.value
        )
        return ConfigOptionReply(result)


@dataclass(frozen=True, kw_only=True)
class CompactRuntimeRequest(ResultRuntimeRequest):
    instructions: str | None = None

    async def result(self, ctx: RuntimeRequestContext) -> CompactionOutcome:
        from .manual_compaction_bridge import compact_context

        return await compact_context(ctx.server.agent.turns, ctx.session_id, self.instructions)


@dataclass(frozen=True, kw_only=True)
class InputDispositionsRuntimeRequest(ResultRuntimeRequest):
    include_history: bool = False

    async def result(self, ctx: RuntimeRequestContext) -> DeliveryOverview:
        return await Coordination.run_worker(partial(ctx.server.agent._comms.goals.input_delivery,
            ctx.name,
            include_history=self.include_history,
            awaiting_keys=await ctx.server.agent.inputs.awaiting_input_keys(ctx.session_id),
        ))


@dataclass(frozen=True, kw_only=True)
class DismissHistoricalInputsRuntimeRequest(ResultRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> DeliveryOverview:
        result = await Coordination.run_worker(partial(ctx.server.agent._comms.goals.dismiss_historical_inputs,
            ctx.name, awaiting_keys=await ctx.server.agent.inputs.awaiting_input_keys(ctx.session_id)
        ))
        await ctx.server.agent.inputs.emit_input_delivery_changed(ctx.session_id)
        return result


@dataclass(frozen=True, kw_only=True)
class GoalHistoryRuntimeRequest(ResultRuntimeRequest):
    goal_id: str | None = None

    async def result(self, ctx: RuntimeRequestContext) -> GoalHistoryReply:
        history = await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.goal_history, ctx.name, goal_id=self.goal_id,
        ))
        return GoalHistoryReply(tuple(row.recorded() for row in history))


class GoalSnapshotResultRuntimeRequest(ResultRuntimeRequest):
    @abstractmethod
    async def change(self, ctx: RuntimeRequestContext) -> None: ...

    async def result(self, ctx: RuntimeRequestContext) -> GoalSnapshotReply:
        from .acp_extension import GoalSnapshotReply, TurnChangedUpdate, UpdateMetadata
        await self.change(ctx)
        snapshot = await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.goal_snapshot, ctx.name,
        ))
        turn = await Coordination.run_worker(partial(
            ctx.server.agent.turns.turn_state, ctx.session_id,
        ))
        return GoalSnapshotReply.from_snapshot(snapshot, UpdateMetadata.of(TurnChangedUpdate(turn)))


@dataclass(frozen=True, kw_only=True)
class GoalSnapshotRuntimeRequest(GoalSnapshotResultRuntimeRequest):
    async def change(self, ctx: RuntimeRequestContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class GoalRevisionRuntimeRequest(ResultRuntimeRequest):
    goal_id: str
    expected_revision: int


@dataclass(frozen=True, kw_only=True)
class GoalTextRuntimeRequest(ResultRuntimeRequest):
    text: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("A goal requires text.")


@dataclass(frozen=True, kw_only=True)
class EditGoalRuntimeRequest(
    GoalTextRuntimeRequest, GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest
):
    async def change(self, ctx: RuntimeRequestContext) -> None:
        await ctx.server.agent.turns.goals.edit_goal(
            ctx.session_id, self.goal_id, self.expected_revision, self.text
        )


@dataclass(frozen=True, kw_only=True)
class UpdateGoalRuntimeRequest(GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest):
    status: type[GoalAction]

    async def change(self, ctx: RuntimeRequestContext) -> None:
        await ctx.server.agent.turns.goals.update_goal(
            ctx.session_id, self.status, self.goal_id, self.expected_revision
        )


@dataclass(frozen=True, kw_only=True)
class RetryGoalRuntimeRequest(GoalRevisionRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> GoalReply:
        goal = await ctx.server.agent.turns.goals.retry_goal(
            ctx.session_id, self.goal_id, self.expected_revision
        )
        return GoalReply(goal)


@dataclass(frozen=True, kw_only=True)
class SetGoalRuntimeRequest(GoalTextRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> GoalReply:
        goal = await ctx.server.agent.turns.goals.set_goal(ctx.session_id, self.text)
        return GoalReply(goal)


@dataclass(frozen=True, kw_only=True)
class ContextRuntimeRequest(ResultRuntimeRequest):
    async def inspect(self, ctx):
        agent = ctx.server.agent
        owner = await Coordination.run_worker(partial(agent._comms.registry.require, ctx.name))
        context = await agent.turns.inspect_context(ctx.session_id, owner)
        return context.require_session_file(owner.runtime.path)

    async def result(self, ctx):
        return await self.inspect(ctx)


@dataclass(frozen=True, kw_only=True)
class ContextSourceRuntimeRequest(ContextRuntimeRequest):
    observation: PreviewProvenance
    segment: int
    source: Provenance

    async def result(self, ctx):
        context = await self.inspect(ctx)
        text = await Coordination.run_worker(partial(
            context.public_source_text, ctx.server.agent._comms,
            self.observation, self.segment, self.source,
        ))
        return text


@dataclass(frozen=True, kw_only=True)
class ContextCoreRuntimeRequest(ResultRuntimeRequest):
    def read_context(self, ctx):
        comms = ctx.server.agent._comms
        owner = self.require_owner(comms.registry.entry(ctx.name).entry)
        return TurnContext.for_inspection(comms, owner)

    async def inspect(self, ctx):
        return await Coordination.run_worker(partial(self.read_context, ctx))

    async def result(self, ctx):
        return await self.inspect(ctx)


@dataclass(frozen=True, kw_only=True)
class ContextCoreSourceRuntimeRequest(ContextCoreRuntimeRequest):
    owner: ThreadIncarnation
    segment: int
    manifest: SegmentManifest
    source: Provenance

    def read_source(self, ctx):
        context = self.read_context(ctx)
        return context.public_source_text(ctx.server.agent._comms, self.owner,
                                        self.segment, self.manifest, self.source)

    async def result(self, ctx):
        return await Coordination.run_worker(partial(self.read_source, ctx))


@dataclass(frozen=True, kw_only=True)
class RecordedContextRuntimeRequest(ResultRuntimeRequest):
    turn: RecordedContextTurn
    request_id: str
    record: ContextManifestReference
    segment: int

    def read_manifest(self, ctx):
        comms = ctx.server.agent._comms
        manifest = self.record.read(comms.bus.log, ctx.name, comms.registry)
        return ContextManifest.for_request((manifest,), self.turn, self.request_id)

    async def manifest(self, ctx):
        return await Coordination.run_worker(partial(self.read_manifest, ctx))


@dataclass(frozen=True, kw_only=True)
class ContextReferenceRuntimeRequest(RecordedContextRuntimeRequest):
    source: Provenance

    async def result(self, ctx):
        manifest = await self.manifest(ctx)
        text = await Coordination.run_worker(partial(
            manifest.public_source_text, ctx.server.agent._comms, self.segment, self.source,
        ))
        return text


@dataclass(frozen=True, kw_only=True)
class ContextRecordedSegmentRuntimeRequest(RecordedContextRuntimeRequest):
    contributors: tuple[int, ...] = ()

    async def selected_segment(self, ctx):
        manifest = await self.manifest(ctx)
        return manifest.selected_segment(self.segment, self.contributors)

    async def result(self, ctx):
        segment = await self.selected_segment(ctx)
        agent = ctx.server.agent
        owner = await Coordination.run_worker(partial(agent._comms.registry.require, ctx.name))

        async def read_reference(original):
            return await agent.turns.inspect_context_segment(ctx.session_id, owner, original)

        text = await segment.public_text(read_reference)
        return ContextSourceText(segment.public_description(), text)


@dataclass(frozen=True, kw_only=True)
class ContextAnnotationsRuntimeRequest(ContextRecordedSegmentRuntimeRequest):
    classifier: ClassifierVersion

    async def result(self, ctx):
        from .working_memory_annotations import WorkingMemoryAnnotations

        segment = await self.selected_segment(ctx)
        labels = await Coordination.run_worker(partial(WorkingMemoryAnnotations.for_segment,
            ctx.server.agent._comms.root / "coordination.sqlite3",
            segment, self.classifier))
        return AnnotationsReply(labels)


@dataclass(frozen=True, kw_only=True)
class ContextAnnotationCorrectionRuntimeRequest(ContextRecordedSegmentRuntimeRequest):
    label: ModelLabel
    answer: type[SpanAnswer]
    worktree: str

    async def result(self, ctx):
        from .cli_commands import CorrectAnnotationCliCommand

        segment = await self.selected_segment(ctx)
        if not segment.contains_span(self.label.span):
            raise ValueError("Correction belongs to another original context source")
        request = CorrectAnnotationCliCommand(
            label=self.label, answer=self.answer, worktree=self.worktree)
        return await Coordination.run_worker(partial(request.apply, ctx.server.agent._comms))
