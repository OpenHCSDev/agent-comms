"""Declaration-owned commands for the existing local owner socket protocol.

Only the socket boundary translates the external ``action`` tag. Request
parameters use A2; membership and dispatch use A1; execution uses A5. These
commands do not own coordination, backend or identity lifecycles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Annotated, ClassVar, Self

from acp.exceptions import RequestError
from acp.schema import PromptResponse, SetSessionConfigOptionResponse
from pydantic import BaseModel, JsonValue

from agent_comms.acp_extension import GoalSnapshotReply
from agent_comms.agent_event_updates import AcpEventConsumer
from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import Refused
from agent_comms.coordination.working_memory_annotations import WorkingMemoryAnnotations
from agent_comms.errors import RelationViolationError
from agent_comms.execution.acp_request_consumer import AcpRequestConsumer
from agent_comms.execution.controller import NO_CONTROLLER
from agent_comms.execution.manual_compaction_bridge import compact_context
from agent_comms.execution.owner_protocol import (
    ClientLine,
    OwnerAnnouncement,
    OwnerFailure,
    OwnerReady,
)
from agent_comms.execution.turn_context import ContextManifest, ContextSourceText
from agent_comms.messaging.bus_read import OwnerReadRuntimeRequest, OwnerScopedRead
from agent_comms.nominal.field_codec import FieldCodec, WireValue
from agent_comms.pi_payloads import NativeContextData
from agent_comms.planning.goal_history import RecordedGoalHistory
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.transport.owner_connection import NoControllerToken, OwnerConnection
from agent_comms.transport.project_request import ProjectRuntimeRequest
from agent_comms.transport.runtime_requests import (
    CancelRuntimeRequest,
    ClearQueueRuntimeRequest,
    CompactRuntimeRequest,
    ContextAnnotationCorrectionRuntimeRequest,
    ContextAnnotationsRuntimeRequest,
    ContextCoreRuntimeRequest,
    ContextCoreSourceRuntimeRequest,
    ContextRecordedSegmentRuntimeRequest,
    ContextReferenceRuntimeRequest,
    ContextRuntimeRequest,
    ContextSourceRuntimeRequest,
    DismissHistoricalInputsRuntimeRequest,
    EditGoalRuntimeRequest,
    FreshSubscribeRuntimeRequest,
    GoalHistoryRuntimeRequest,
    GoalRevisionRuntimeRequest,
    GoalSnapshotResultRuntimeRequest,
    GoalTextRuntimeRequest,
    PromptRuntimeRequest,
    RecordedContextRuntimeRequest,
    RetryGoalRuntimeRequest,
    SendNowRuntimeRequest,
    SetConfigOptionRuntimeRequest,
    SetGoalRuntimeRequest,
    SubscribeRuntimeRequest,
    UpdateGoalRuntimeRequest,
)
from agent_comms.transport.runtime_server import (
    AttachedRuntimeRequestContext,
    ServedAttachedResultRuntimeRequest,
    ServedAttachedRuntimeRequest,
    SubscriberClosed,
)
from agent_comms.transport.tool_result import DeclaredReply, ToolResult
from agent_comms.transport.tools.reads import OwnerGoalRead
from agent_comms.transport.tools.sidebar import ClearQueueQueueAction, SendNowQueueAction
from agent_comms.ui_model.observation import SubscribeTranscript
from agent_comms.working_memory.working_memory_labels import ModelLabel


class OwnedRead[Result: ToolResult = ToolResult](OwnerScopedRead[Result], ABC):
    @abstractmethod
    async def owned(self, ctx: AttachedRuntimeRequestContext) -> Result: ...


class OwnerLiveRead[Result: ToolResult = ToolResult](OwnedRead[Result], ABC):
    """This subfamily has no answer without the selected live native owner."""

    def unavailable(self, comms: Comms, owner: ThreadIncarnation) -> Result:
        raise RelationViolationError("The read requires the live native owner.")


class ServedOwnerReadRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    OwnerReadRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> ToolResult:
        return await OwnedRead.serving(self.operation).owned(ctx)


from agent_comms.agent_backend import CompactionOutcome
from agent_comms.input_disposition import DeliveryOverview


@dataclass(frozen=True)
class ProjectReply(DeclaredReply):
    worktree: Checkout


@dataclass(frozen=True)
class CancelReply(DeclaredReply):
    pass


@dataclass(frozen=True)
class QueueActionReply(DeclaredReply):
    """The owner applied a queue action to its session's queued inputs.

    Undeclared as a record, the codec could not encode it: every send_now and clear_queue ran,
    then closed without its reply, and Toad showed a steer that worked as "outcome unknown"."""


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


class ServedProjectRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    ProjectRuntimeRequest,
):
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


class ServedSubscribeRuntimeRequest(
    ServedAttachedRuntimeRequest,
    SubscribeRuntimeRequest,
):
    @asynccontextmanager
    async def observing(self, ctx: AttachedRuntimeRequestContext) -> AsyncIterator[None]:
        agent = ctx.server.agent
        ctx.server.clients.setdefault(ctx.session, set()).add(ctx.client)
        await agent.emit_session_identity(ctx.session, ctx.name, client=ctx.client)
        await agent.sessions.transcript.replay(
            ctx.name, session_id=ctx.session, client=ctx.client, read=SubscribeTranscript,
        )
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


class ServedFreshSubscribeRuntimeRequest(
    ServedSubscribeRuntimeRequest,
    FreshSubscribeRuntimeRequest,
):
    @asynccontextmanager
    async def subscribed(self, ctx: AttachedRuntimeRequestContext) -> AsyncIterator[None]:
        """A new ID is announced before its first publication can reach the ACP attachment."""
        await self.ready(ctx)
        async with self.observing(ctx):
            yield


class ServedPromptRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    PromptRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> PromptReply:
        controller = next(
            (
                subscriber
                for subscriber in ctx.server.clients.get(ctx.session, ())
                if subscriber.token == self.controller_token and not subscriber.writer.is_closing()
            ),
            NO_CONTROLLER,
        )
        token = ctx.server.controller.set(controller)
        try:
            with ctx.server.agent.refusals():
                result = await AcpRequestConsumer(ctx.server.agent, self.request, ctx.session).run()
        finally:
            ctx.server.controller.reset(token)
        return PromptReply(result)

    async def apply(self, ctx: AttachedRuntimeRequestContext) -> None:
        """Only a prompt runs through CommsAgent's ACP methods, so only it fails in protocol form.

        The failed turn crosses whole, and the client's own boundary raises it as itself.
        Closing without a reply would report it as an unknown outcome.
        """
        try:
            await super().apply(ctx)
        except RequestError as error:
            with suppress(SubscriberClosed):
                await ctx.send(OwnerFailure.refused(Refused(error.code, str(error), error.data)))
            raise


class ServedCancelRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    CancelRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> CancelReply:
        await ctx.server.agent.turns.cancel(ctx.session)
        return CancelReply()


class ServedSendNowRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    SendNowRuntimeRequest,
    action=SendNowQueueAction,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> QueueActionReply:
        await ctx.server.agent.inputs.send_now(ctx.session)
        return QueueActionReply()


class ServedClearQueueRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    ClearQueueRuntimeRequest,
    action=ClearQueueQueueAction,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> QueueActionReply:
        await ctx.server.agent.inputs.clear_queued_inputs(ctx.session)
        return QueueActionReply()


class ServedSetConfigOptionRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    SetConfigOptionRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> ConfigOptionReply:
        result = await ctx.server.agent.sessions.config.set_option(
            self.config_id, ctx.session, self.value
        )
        return ConfigOptionReply(result)


class ServedCompactRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    CompactRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> CompactionOutcome:
        return await compact_context(ctx.server.agent.turns, ctx.session, self.instructions)


class ServedDismissHistoricalInputsRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    DismissHistoricalInputsRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> DeliveryOverview:
        result = await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.dismiss_historical_inputs,
            ctx.name, scope=await ctx.server.agent.inputs.delivery_scope(ctx.session),
        ))
        await ctx.server.agent.inputs.emit_input_delivery_changed(ctx.session)
        return result


class ServedGoalHistoryRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    GoalHistoryRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> RecordedGoalHistory:
        return await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.recorded_history, ctx.name, self.goal_id,
        ))


class ServedGoalSnapshotResultRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    GoalSnapshotResultRuntimeRequest,
):
    @abstractmethod
    async def change(self, ctx: AttachedRuntimeRequestContext) -> None: ...

    async def result(self, ctx: AttachedRuntimeRequestContext) -> GoalSnapshotReply:
        await self.change(ctx)
        return await OwnedRead[GoalSnapshotReply].serving(OwnerGoalRead()).owned(ctx)


class ServedGoalRevisionRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    GoalRevisionRuntimeRequest,
):
    pass


class ServedGoalTextRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    GoalTextRuntimeRequest,
):
    pass


class ServedEditGoalRuntimeRequest(
    ServedGoalTextRuntimeRequest,
    ServedGoalRevisionRuntimeRequest,
    ServedGoalSnapshotResultRuntimeRequest,
    EditGoalRuntimeRequest,
):
    async def change(self, ctx: AttachedRuntimeRequestContext) -> None:
        await ctx.server.agent.turns.goals.edit_goal(
            ctx.session, self.goal_id, self.expected_revision, self.text
        )


class ServedUpdateGoalRuntimeRequest(
    ServedGoalRevisionRuntimeRequest,
    ServedGoalSnapshotResultRuntimeRequest,
    UpdateGoalRuntimeRequest,
):
    async def change(self, ctx: AttachedRuntimeRequestContext) -> None:
        await ctx.server.agent.turns.goals.update_goal(
            ctx.session, self.status, self.goal_id, self.expected_revision
        )


class ServedRetryGoalRuntimeRequest(
    ServedGoalRevisionRuntimeRequest,
    RetryGoalRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> GoalReply:
        goal = await ctx.server.agent.turns.goals.retry_goal(
            ctx.session, self.goal_id, self.expected_revision
        )
        return GoalReply(goal)


class ServedSetGoalRuntimeRequest(ServedGoalTextRuntimeRequest, SetGoalRuntimeRequest):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> GoalReply:
        goal = await ctx.server.agent.turns.goals.set_goal(ctx.session, self.text)
        return GoalReply(goal)


class ServedContextRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    ContextRuntimeRequest,
):
    async def inspect(self, ctx: AttachedRuntimeRequestContext) -> NativeContextData:
        agent = ctx.server.agent
        owner = await Coordination.run_worker(partial(agent._comms.registry.require, ctx.name))
        context = await agent.turns.inspect_context(ctx.session, owner)
        return context.require_session_file(owner.runtime.path)

    async def result(self, ctx: AttachedRuntimeRequestContext):
        return await self.inspect(ctx)


class ServedContextSourceRuntimeRequest(
    ServedContextRuntimeRequest,
    ContextSourceRuntimeRequest,
):
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


class ServedContextCoreRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    ContextCoreRuntimeRequest,
):
    def read_context(self, ctx: AttachedRuntimeRequestContext):
        comms = ctx.server.agent._comms
        owner = self.require_owner(comms.registry.entry(ctx.name).entry)
        return comms.context_for(owner)

    async def inspect(self, ctx: AttachedRuntimeRequestContext):
        return await Coordination.run_worker(partial(self.read_context, ctx))

    async def result(self, ctx: AttachedRuntimeRequestContext):
        return await self.inspect(ctx)


class ServedContextCoreSourceRuntimeRequest(
    ServedContextCoreRuntimeRequest,
    ContextCoreSourceRuntimeRequest,
):
    def read_source(self, ctx: AttachedRuntimeRequestContext):
        context = self.read_context(ctx)
        return context.public_source_text(ctx.server.agent._comms, self.owner,
                                        self.segment, self.manifest, self.source)

    async def result(self, ctx: AttachedRuntimeRequestContext):
        return await Coordination.run_worker(partial(self.read_source, ctx))


class ServedRecordedContextRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    RecordedContextRuntimeRequest,
):
    def read_manifest(self, ctx: AttachedRuntimeRequestContext):
        comms = ctx.server.agent._comms
        manifest = self.record.read(comms.contexts, ctx.name, comms.registry)
        return ContextManifest.for_request((manifest,), self.turn, self.request_id)

    async def manifest(self, ctx: AttachedRuntimeRequestContext):
        return await Coordination.run_worker(partial(self.read_manifest, ctx))


class ServedContextReferenceRuntimeRequest(
    ServedRecordedContextRuntimeRequest,
    ContextReferenceRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext):
        manifest = await self.manifest(ctx)
        text = await Coordination.run_worker(partial(
            manifest.public_source_text, ctx.server.agent._comms, self.segment, self.source,
        ))
        return text


class ServedContextRecordedSegmentRuntimeRequest(
    ServedRecordedContextRuntimeRequest,
    ContextRecordedSegmentRuntimeRequest,
):
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


class ServedContextAnnotationsRuntimeRequest(
    ServedContextRecordedSegmentRuntimeRequest,
    ContextAnnotationsRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext):
        segment = await self.selected_segment(ctx)
        labels = await Coordination.run_worker(partial(WorkingMemoryAnnotations.for_segment,
            ctx.server.agent._comms.registry.store.coordinator_reader,
            segment, self.classifier))
        return AnnotationsReply(labels)


class ServedContextAnnotationCorrectionRuntimeRequest(
    ServedContextRecordedSegmentRuntimeRequest,
    ContextAnnotationCorrectionRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext):
        segment = await self.selected_segment(ctx)
        if not segment.contains_span(self.label.span):
            raise ValueError("Correction belongs to another original context source")
        return await Coordination.run_worker(partial(self.correct, ctx.server.agent._comms))
