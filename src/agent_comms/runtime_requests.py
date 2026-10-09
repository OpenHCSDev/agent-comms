"""Declaration-owned commands for the existing local owner socket protocol.

Only the socket boundary translates the external ``action`` tag. Request
parameters use A2; membership and dispatch use A1; execution uses A5. These
commands do not own coordination, backend or identity lifecycles.
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import abstractmethod
from functools import partial
from .coordinator import Coordination
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Self

from .command import Command
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .goal_actions import GoalAction
from .thread_presentation import LiveThreadOwnerBinding
from .turn_context import ContextManifest, ContextSourceText, PreviewProvenance, Provenance, RecordedContextTurn, SegmentManifest, TurnContext
from .thread_identity import ThreadIncarnation
from .working_memory_labels import ClassifierVersion, ModelLabel
from .working_memory_questions import SpanAnswer

if TYPE_CHECKING:
    from .runtime import RuntimeServer, SocketClient


@dataclass(frozen=True)
class RuntimeRequestContext:
    server: RuntimeServer
    reader: asyncio.StreamReader
    client: SocketClient
    session_id: str
    name: str

    async def send(self, payload: dict[str, Any]) -> None:
        await self.client.send(payload)


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

    def to_wire(self) -> dict[str, Any]:
        payload = FieldCodec.encode(self)
        return {"action": payload.pop("kind"), **payload}

    @classmethod
    def proxy_payload(
        cls, thread: str, controller_token: str | None, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        # The owner decodes once; proxies preserve the owner's error envelope.
        return {"action": FieldCodec.encode(cls), "thread": thread,
                **FieldCodec.encode(parameters)}

    def require_owner(self, snapshot):
        owner = snapshot.require(self.thread)
        if owner.pid != os.getpid() or not snapshot.status(owner.name).running:
            raise RuntimeError("This process no longer owns the thread.")
        return owner

    async def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient
    ) -> RuntimeRequestContext:
        snapshot = await Coordination.run_worker(server.agent._comms.registry.snapshot)
        owner = self.require_owner(snapshot)
        session_id = server.agent.sessions.require_owned_session(owner, snapshot)
        return RuntimeRequestContext(server, reader, client, session_id, owner.name)


class ResultRuntimeRequest(RuntimeRequest):
    async def apply(self, ctx: RuntimeRequestContext) -> None:
        await ctx.send({"result": await self.result(ctx)})

    @abstractmethod
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]: ...


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

    def require_original(self, snapshot):
        if not self.binding.owner.incarnation.matches_recorded_name(self.thread, snapshot):
            raise ValueError("Project request names another original thread")
        snapshot.require_owner_process(self.binding.owner, self.binding.process)
        return snapshot.require_active(self.binding.owner.incarnation.name)

    def require_owner(self, snapshot):
        self.require_original(snapshot)
        return super().require_owner(snapshot)

    async def result(self, ctx):
        snapshot = await Coordination.run_worker(ctx.server.agent._comms.registry.snapshot)
        current = self.require_original(snapshot)
        return {"worktree": current.worktree}


@dataclass(frozen=True, kw_only=True)
class SubscribeRuntimeRequest(RuntimeRequest):
    async def apply(self, ctx: RuntimeRequestContext) -> None:
        agent = ctx.server.agent
        ctx.server.clients.setdefault(ctx.session_id, set()).add(ctx.client)
        await agent.emit_session_identity(ctx.session_id, ctx.name, client=ctx.client)
        await agent.sessions.transcript.replay(
            ctx.session_id,
            ctx.name,
            client=ctx.client,
        )
        await agent.turns.replay_turn_state(ctx.session_id, client=ctx.client)
        await agent.inputs.replay_unknown_inputs(ctx.session_id, client=ctx.client)
        async with agent.sessions.config.session_options(ctx.name) as config_options:
            metadata = await agent.sessions.metadata(ctx.name)
            await ctx.send(
                {
                    "controllerToken": ctx.client.token,
                    "ready": {
                        **metadata,
                        "configOptions": [
                            option.model_dump(by_alias=True, exclude_none=True)
                            for option in config_options
                        ],
                    },
                }
            )
        while line := await ctx.reader.readline():
            response = json.loads(line)
            receipt = response.get("permissionResponse") if isinstance(response, dict) else None
            if not isinstance(receipt, dict):
                continue
            reply_id = receipt.get("id")
            pending = ctx.client.pending.get(reply_id) if isinstance(reply_id, str) else None
            if pending is not None and not pending.done():
                pending.set_result(receipt)


@dataclass(frozen=True, kw_only=True)
class PromptRuntimeRequest(ResultRuntimeRequest):
    prompt: list[dict[str, Any]]
    meta: dict[str, Any] | None = None
    controller_token: str | None = field(default=None, metadata={"wire_name": "controllerToken"})

    @classmethod
    def proxy_payload(
        cls, thread: str, controller_token: str | None, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        return super().proxy_payload(
            thread, controller_token, {"controllerToken": controller_token, **parameters}
        )

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
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
        return result.model_dump(by_alias=True, exclude_none=True)


@dataclass(frozen=True, kw_only=True)
class CancelRuntimeRequest(ResultRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        await ctx.server.agent.cancel(ctx.session_id)
        return {}


@dataclass(frozen=True, kw_only=True)
class SetConfigOptionRuntimeRequest(ResultRuntimeRequest):
    config_id: str
    value: str | bool

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        result = await ctx.server.agent.set_config_option(
            self.config_id, ctx.session_id, self.value
        )
        return result.model_dump(by_alias=True, exclude_none=True)


@dataclass(frozen=True, kw_only=True)
class CompactRuntimeRequest(ResultRuntimeRequest):
    instructions: str | None = None

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        from .manual_compaction_bridge import compact_context

        return FieldCodec.encode(
            await compact_context(ctx.server.agent.turns, ctx.session_id, self.instructions)
        )


@dataclass(frozen=True, kw_only=True)
class InputDispositionsRuntimeRequest(ResultRuntimeRequest):
    include_history: bool = False

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        return await Coordination.run_worker(partial(ctx.server.agent._comms.goals.input_delivery,
            ctx.name,
            include_history=self.include_history,
            awaiting_keys=await ctx.server.agent.inputs.awaiting_input_keys(ctx.session_id),
        ))


@dataclass(frozen=True, kw_only=True)
class DismissHistoricalInputsRuntimeRequest(ResultRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        result = await Coordination.run_worker(partial(ctx.server.agent._comms.goals.dismiss_historical_inputs,
            ctx.name, awaiting_keys=await ctx.server.agent.inputs.awaiting_input_keys(ctx.session_id)
        ))
        await ctx.server.agent.inputs.emit_input_delivery_changed(ctx.session_id)
        return result


@dataclass(frozen=True, kw_only=True)
class GoalHistoryRuntimeRequest(ResultRuntimeRequest):
    goal_id: str | None = None

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        history = await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.goal_history, ctx.name, goal_id=self.goal_id,
        ))
        return {"history": [row.to_wire() for row in history]}


class GoalSnapshotResultRuntimeRequest(ResultRuntimeRequest):
    @abstractmethod
    async def change(self, ctx: RuntimeRequestContext) -> None: ...

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        from .acp_extension import TurnChangedUpdate, encode_updates
        await self.change(ctx)
        goal, execution = await Coordination.run_worker(partial(
            ctx.server.agent._comms.goals.goal_snapshot, ctx.name,
        ))
        return {
            "goal": goal.to_wire() if goal is not None else None,
            "goalExecution": asdict(execution) if execution is not None else None,
            "_meta": encode_updates(TurnChangedUpdate(await Coordination.run_worker(partial(
                ctx.server.agent.turns.turn_state, ctx.session_id,
            )))),
        }


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
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        goal = await ctx.server.agent.turns.goals.retry_goal(
            ctx.session_id, self.goal_id, self.expected_revision
        )
        return {"goal": goal.to_wire()}


@dataclass(frozen=True, kw_only=True)
class SetGoalRuntimeRequest(GoalTextRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        goal = await ctx.server.agent.turns.goals.set_goal(ctx.session_id, self.text)
        return {"goal": goal.to_wire()}


@dataclass(frozen=True, kw_only=True)
class ContextRuntimeRequest(ResultRuntimeRequest):
    async def inspect(self, ctx):
        agent = ctx.server.agent
        owner = await Coordination.run_worker(partial(agent._comms.registry.require, ctx.name))
        context = await agent.turns.inspect_context(ctx.session_id, owner)
        return context.require_session_file(owner.require_saved_session())

    async def result(self, ctx):
        return FieldCodec.encode(await self.inspect(ctx))


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
        return FieldCodec.encode(text)


@dataclass(frozen=True, kw_only=True)
class ContextCoreRuntimeRequest(ResultRuntimeRequest):
    def read_context(self, ctx):
        comms = ctx.server.agent._comms
        owner = self.require_owner(comms.registry.snapshot())
        return TurnContext.for_inspection(comms, owner)

    async def inspect(self, ctx):
        return await Coordination.run_worker(partial(self.read_context, ctx))

    async def result(self, ctx):
        return FieldCodec.encode(await self.inspect(ctx))


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
        return FieldCodec.encode(await Coordination.run_worker(partial(self.read_source, ctx)))


@dataclass(frozen=True, kw_only=True)
class RecordedContextRuntimeRequest(ResultRuntimeRequest):
    turn: RecordedContextTurn
    request_id: str
    segment: int

    def read_manifest(self, ctx):
        comms = ctx.server.agent._comms
        history = comms.bus.log.context_manifests(ctx.name, comms.registry)
        return ContextManifest.for_request(history, self.turn, self.request_id)

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
        return FieldCodec.encode(text)


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
        return FieldCodec.encode(ContextSourceText(segment.public_description(), text))


@dataclass(frozen=True, kw_only=True)
class ContextAnnotationsRuntimeRequest(ContextRecordedSegmentRuntimeRequest):
    classifier: ClassifierVersion

    async def result(self, ctx):
        from .working_memory_annotations import WorkingMemoryAnnotations

        segment = await self.selected_segment(ctx)
        labels = await Coordination.run_worker(partial(WorkingMemoryAnnotations.for_segment,
            ctx.server.agent._comms.root / "coordination.sqlite3",
            segment, self.classifier))
        return FieldCodec.encode(labels)


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
        corrected = await Coordination.run_worker(partial(request.apply, ctx.server.agent._comms))
        return FieldCodec.encode(corrected)
