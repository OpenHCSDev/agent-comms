"""Declaration-owned commands for the existing local owner socket protocol.

Only the socket boundary translates the external ``action`` tag. Request
parameters use A2; membership and dispatch use A1; execution uses A5. These
commands do not own coordination, backend or identity lifecycles.
"""

from __future__ import annotations

from .repo import Checkout

import json
from abc import abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from acp.schema import PromptRequest, PromptResponse, SetSessionConfigOptionResponse
from pydantic import BaseModel, JsonValue

from .acp_extension import (
    GoalSnapshotReply,
)

from .agent_event_updates import AcpEventConsumer
from .acp_request_consumer import AcpRequestConsumer
from agent_comms.cli_verbs.correct_annotation import CorrectAnnotationCliCommand
from .context_store import ContextManifestReference
from .coordinator import Coordination
from .errors import RelationViolationError
from .field_codec import FieldCodec, WireName, WireValue, GoalId
from .goal_actions import GoalAction
from .goal_history import RecordedGoalHistory
from .goal_scope import AllGoals, OneGoal
from .goals import Goal
from .manual_compaction_bridge import compact_context
from .order_tools import OwnerGoalRead
from agent_comms.runtime_server import AttachedResultRuntimeRequest, AttachedRuntimeRequest, AttachedRuntimeRequestContext
from .owner_connection import NoControllerToken
from .owner_protocol import ClientLine, OwnerAnnouncement, OwnerReady
from .pi_payloads import NativeContextData
from .thread_identity import ThreadIncarnation
from .thread_presentation import LiveThreadOwnerBinding
from .tool_result import DeclaredReply
from .turn_context import (
    ContextManifest,
    ContextSourceText,
    PreviewProvenance,
    Provenance,
    RecordedContextTurn,
    SegmentManifest,
    TurnContext,
)
from .working_memory_annotations import WorkingMemoryAnnotations
from .working_memory_labels import ClassifierVersion, ModelLabel
from .working_memory_questions import SpanAnswer
from .turn_item import PiRequestKey

if TYPE_CHECKING:
    from .agent_backend import CompactionOutcome
    from .input_disposition import DeliveryOverview


@dataclass(frozen=True)
class ProjectReply(DeclaredReply):
    worktree: Checkout


@dataclass(frozen=True)
class CancelReply(DeclaredReply):
    pass


@dataclass(frozen=True)
class GoalReply(DeclaredReply):
    goal: Goal


@dataclass(frozen=True)
class AcpResponseReply(DeclaredReply, WireValue):
    """An ACP schema response, sent exactly as the schema writes it."""

    response_type: ClassVar[type[BaseModel]]
    response: BaseModel

    def to_wire(self) -> JsonValue:
        return FieldCodec.encode(self.response)

    @classmethod
    def from_wire(cls, data: dict[str, JsonValue]) -> Self:
        return cls(FieldCodec.decode(cls.response_type, data))


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
class ProjectRuntimeRequest(AttachedResultRuntimeRequest):
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
        from agent_comms.owner_connection import socket_path

        return {
            "AGENT_COMMS_PROJECT_SOCKET": str(socket_path(root, self.binding.process.pid)),
            "AGENT_COMMS_PROJECT_REQUEST": json.dumps(FieldCodec.encode(self)),
        }

    def require_original(self, entry):
        if not entry.matches_name(self.thread):
            raise ValueError("Project request names another original thread")
        return entry.require_owner_process(self.binding.owner, self.binding.process)

    def require_owner(self, entry):
        self.require_original(entry)
        return super().require_owner(entry)

    async def result(self, ctx: AttachedRuntimeRequestContext) -> ProjectReply:
        entry = (await Coordination.run_worker(partial(
            ctx.server.agent._comms.registry.entry, ctx.name,
        ))).entry
        current = self.require_original(entry)
        return ProjectReply(current.worktree)


@dataclass(frozen=True, kw_only=True)
class SubscribeRuntimeRequest(AttachedRuntimeRequest):
    @asynccontextmanager
    async def observing(self, ctx: AttachedRuntimeRequestContext) -> AsyncIterator[None]:
        agent = ctx.server.agent
        ctx.server.clients.setdefault(ctx.session, set()).add(ctx.client)
        await agent.emit_session_identity(ctx.session, ctx.name, client=ctx.client)
        async with agent.sessions.transcript.subscribe(self.thread, client=ctx.client):
            await agent.turns.publish_turn_state(
                AcpEventConsumer(agent, ctx.session, ctx.client)
            )
            await agent.inputs.emit_input_delivery_changed(ctx.session, client=ctx.client)
            yield

    async def ready(self, ctx: AttachedRuntimeRequestContext) -> None:
        await ctx.send(OwnerReady(
            ready=await ctx.server.agent.sessions.metadata(ctx.name, session_id=ctx.session),
            controller_token=ctx.client.token,
        ))

    @asynccontextmanager
    async def subscribed(self, ctx: AttachedRuntimeRequestContext) -> AsyncIterator[None]:
        """A load replays history before its response, under its already-known ID."""
        async with self.observing(ctx):
            await self.ready(ctx)
            yield

    async def apply(self, ctx: AttachedRuntimeRequestContext) -> None:
        agent = ctx.server.agent
        await ctx.send(OwnerAnnouncement())
        async with self.subscribed(ctx):
            async with agent.sessions.config.observe_configuration(ctx.session, ctx.client):
                while line := await ctx.reader.readline():
                    ClientLine.read(line).answer(ctx.client)


class FreshSubscribeRuntimeRequest(SubscribeRuntimeRequest):
    @asynccontextmanager
    async def subscribed(self, ctx: AttachedRuntimeRequestContext) -> AsyncIterator[None]:
        """A new ID is announced before its first publication can reach the ACP attachment."""
        await self.ready(ctx)
        async with self.observing(ctx):
            yield


@dataclass(frozen=True, kw_only=True)
class PromptRuntimeRequest(AttachedResultRuntimeRequest):
    request: PromptRequest
    controller_token: Annotated[str | NoControllerToken, WireName("controllerToken")] = (
        NoControllerToken()
    )

    @classmethod
    def proxy_payload(
        cls, thread: str, controller_token: str | NoControllerToken, parameters: dict[str, object]
    ) -> dict[str, object]:
        return super().proxy_payload(
            thread, controller_token, {"controllerToken": controller_token, **parameters}
        )

    async def result(self, ctx: AttachedRuntimeRequestContext) -> PromptReply:
        controller = next(
            (
                subscriber
                for subscriber in ctx.server.clients.get(ctx.session, ())
                if subscriber.token == self.controller_token and not subscriber.writer.is_closing()
            ),
            None,
        )
        token = ctx.server.controller.set(controller)
        try:
            with ctx.server.agent.refusals():
                result = await AcpRequestConsumer(ctx.server.agent, self.request, ctx.session).run()
        finally:
            ctx.server.controller.reset(token)
        return PromptReply(result)


@dataclass(frozen=True, kw_only=True)
class CancelRuntimeRequest(AttachedResultRuntimeRequest):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> CancelReply:
        await ctx.server.agent.turns.cancel(ctx.session)
        return CancelReply()


@dataclass(frozen=True, kw_only=True)
class SetConfigOptionRuntimeRequest(AttachedResultRuntimeRequest):
    config_id: str
    value: str | bool

    async def result(self, ctx: AttachedRuntimeRequestContext) -> ConfigOptionReply:
        result = await ctx.server.agent.sessions.config.set_option(
            self.config_id, ctx.session, self.value
        )
        return ConfigOptionReply(result)


@dataclass(frozen=True, kw_only=True)
class CompactRuntimeRequest(AttachedResultRuntimeRequest):
    instructions: str | None = None

    async def result(self, ctx: AttachedRuntimeRequestContext) -> CompactionOutcome:
        return await compact_context(ctx.server.agent.turns, ctx.session, self.instructions)


@dataclass(frozen=True, kw_only=True)
class DismissHistoricalInputsRuntimeRequest(AttachedResultRuntimeRequest):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> DeliveryOverview:
        result = await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.dismiss_historical_inputs,
            ctx.name, scope=await ctx.server.agent.inputs.delivery_scope(ctx.session),
        ))
        await ctx.server.agent.inputs.emit_input_delivery_changed(ctx.session)
        return result


@dataclass(frozen=True, kw_only=True)
class GoalHistoryRuntimeRequest(AttachedResultRuntimeRequest):
    goal_id: OneGoal | AllGoals = AllGoals()

    async def result(self, ctx: AttachedRuntimeRequestContext) -> RecordedGoalHistory:
        return await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.recorded_history, ctx.name, self.goal_id,
        ))


class GoalSnapshotResultRuntimeRequest(AttachedResultRuntimeRequest):
    @abstractmethod
    async def change(self, ctx: AttachedRuntimeRequestContext) -> None: ...

    async def result(self, ctx: AttachedRuntimeRequestContext) -> GoalSnapshotReply:
        await self.change(ctx)
        return await OwnerGoalRead().owned(ctx)


@dataclass(frozen=True, kw_only=True)
class GoalRevisionRuntimeRequest(AttachedResultRuntimeRequest):
    goal_id: GoalId
    expected_revision: int


@dataclass(frozen=True, kw_only=True)
class GoalTextRuntimeRequest(AttachedResultRuntimeRequest):
    text: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("A goal requires text.")


@dataclass(frozen=True, kw_only=True)
class EditGoalRuntimeRequest(
    GoalTextRuntimeRequest, GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest
):
    async def change(self, ctx: AttachedRuntimeRequestContext) -> None:
        await ctx.server.agent.turns.goals.edit_goal(
            ctx.session, self.goal_id, self.expected_revision, self.text
        )


@dataclass(frozen=True, kw_only=True)
class UpdateGoalRuntimeRequest(GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest):
    status: type[GoalAction]

    async def change(self, ctx: AttachedRuntimeRequestContext) -> None:
        await ctx.server.agent.turns.goals.update_goal(
            ctx.session, self.status, self.goal_id, self.expected_revision
        )


@dataclass(frozen=True, kw_only=True)
class RetryGoalRuntimeRequest(GoalRevisionRuntimeRequest):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> GoalReply:
        goal = await ctx.server.agent.turns.goals.retry_goal(
            ctx.session, self.goal_id, self.expected_revision
        )
        return GoalReply(goal)


@dataclass(frozen=True, kw_only=True)
class SetGoalRuntimeRequest(GoalTextRuntimeRequest):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> GoalReply:
        goal = await ctx.server.agent.turns.goals.set_goal(ctx.session, self.text)
        return GoalReply(goal)


@dataclass(frozen=True, kw_only=True)
class ContextRuntimeRequest(AttachedResultRuntimeRequest):
    async def inspect(self, ctx: AttachedRuntimeRequestContext) -> NativeContextData:
        agent = ctx.server.agent
        owner = await Coordination.run_worker(partial(agent._comms.registry.require, ctx.name))
        context = await agent.turns.inspect_context(ctx.session, owner)
        return context.require_session_file(owner.runtime.path)

    async def result(self, ctx: AttachedRuntimeRequestContext):
        return await self.inspect(ctx)


@dataclass(frozen=True, kw_only=True)
class ContextSourceRuntimeRequest(ContextRuntimeRequest):
    observation: PreviewProvenance
    segment: int
    source: Provenance

    def read_source(
        self, ctx: AttachedRuntimeRequestContext, context: NativeContextData,
    ) -> ContextSourceText:
        source = context.public_source(self.observation, self.segment, self.source)
        return ContextSourceText(
            source.public_description(), source.public_text(ctx.server.agent._comms),
        )

    async def result(self, ctx: AttachedRuntimeRequestContext) -> ContextSourceText:
        context = await self.inspect(ctx)
        return await Coordination.run_worker(partial(self.read_source, ctx, context))


@dataclass(frozen=True, kw_only=True)
class ContextCoreRuntimeRequest(AttachedResultRuntimeRequest):
    def read_context(self, ctx: AttachedRuntimeRequestContext):
        comms = ctx.server.agent._comms
        owner = self.require_owner(comms.registry.entry(ctx.name).entry)
        return TurnContext.for_inspection(comms, owner)

    async def inspect(self, ctx: AttachedRuntimeRequestContext):
        return await Coordination.run_worker(partial(self.read_context, ctx))

    async def result(self, ctx: AttachedRuntimeRequestContext):
        return await self.inspect(ctx)


@dataclass(frozen=True, kw_only=True)
class ContextCoreSourceRuntimeRequest(ContextCoreRuntimeRequest):
    owner: ThreadIncarnation
    segment: int
    manifest: SegmentManifest
    source: Provenance

    def read_source(self, ctx: AttachedRuntimeRequestContext):
        context = self.read_context(ctx)
        return context.public_source_text(ctx.server.agent._comms, self.owner,
                                        self.segment, self.manifest, self.source)

    async def result(self, ctx: AttachedRuntimeRequestContext):
        return await Coordination.run_worker(partial(self.read_source, ctx))


@dataclass(frozen=True, kw_only=True)
class RecordedContextRuntimeRequest(AttachedResultRuntimeRequest):
    turn: RecordedContextTurn
    request_id: PiRequestKey
    record: ContextManifestReference
    segment: int

    def read_manifest(self, ctx: AttachedRuntimeRequestContext):
        comms = ctx.server.agent._comms
        manifest = self.record.read(comms.contexts, ctx.name, comms.registry)
        return ContextManifest.for_request((manifest,), self.turn, self.request_id)

    async def manifest(self, ctx: AttachedRuntimeRequestContext):
        return await Coordination.run_worker(partial(self.read_manifest, ctx))


@dataclass(frozen=True, kw_only=True)
class ContextReferenceRuntimeRequest(RecordedContextRuntimeRequest):
    source: Provenance

    async def result(self, ctx: AttachedRuntimeRequestContext):
        manifest = await self.manifest(ctx)
        text = await Coordination.run_worker(partial(
            manifest.public_source_text, ctx.server.agent._comms, self.segment, self.source,
        ))
        return text


@dataclass(frozen=True, kw_only=True)
class ContextRecordedSegmentRuntimeRequest(RecordedContextRuntimeRequest):
    contributors: tuple[int, ...] = ()

    async def selected_segment(self, ctx: AttachedRuntimeRequestContext):
        manifest = await self.manifest(ctx)
        return manifest.selected_segment(self.segment, self.contributors)

    async def result(self, ctx: AttachedRuntimeRequestContext):
        segment = await self.selected_segment(ctx)
        agent = ctx.server.agent
        owner = await Coordination.run_worker(partial(agent._comms.registry.require, ctx.name))

        async def read_reference(original):
            return await agent.turns.inspect_context_segment(ctx.session, owner, original)

        text = await segment.public_text(read_reference)
        return ContextSourceText(segment.public_description(), text)


@dataclass(frozen=True, kw_only=True)
class ContextAnnotationsRuntimeRequest(ContextRecordedSegmentRuntimeRequest):
    classifier: ClassifierVersion

    async def result(self, ctx: AttachedRuntimeRequestContext):
        segment = await self.selected_segment(ctx)
        labels = await Coordination.run_worker(partial(WorkingMemoryAnnotations.for_segment,
            ctx.server.agent._comms.registry.store.coordinator_reader,
            segment, self.classifier))
        return AnnotationsReply(labels)


@dataclass(frozen=True, kw_only=True)
class ContextAnnotationCorrectionRuntimeRequest(ContextRecordedSegmentRuntimeRequest):
    label: ModelLabel
    answer: type[SpanAnswer]
    worktree: Checkout

    async def result(self, ctx: AttachedRuntimeRequestContext):
        segment = await self.selected_segment(ctx)
        if not segment.contains_span(self.label.span):
            raise ValueError("Correction belongs to another original context source")
        request = CorrectAnnotationCliCommand(
            label=self.label, answer=self.answer, worktree=self.worktree)
        return await Coordination.run_worker(partial(request.apply, ctx.server.agent._comms))
