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
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Self

from .command import Command
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .goal_actions import (
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    OwnerControlInvocable,
    OwnerInvocable,
    RetryGoalAction,
    SetGoalAction,
)

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
        self.client.writer.write((json.dumps(payload) + "\n").encode())
        await self.client.writer.drain()


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
        return {"action": cls.declared_name, "thread": thread, **parameters}

    def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient
    ) -> RuntimeRequestContext:
        owner = server.agent._comms.registry.require(self.thread)
        name = owner.name
        if owner.pid != os.getpid() or not server.agent._comms.registry.status(name).running:
            raise RuntimeError("This process no longer owns the thread.")
        session_id = next(
            key
            for key, value in server.agent.sessions.bindings.items()
            if server.agent._comms.registry.canonical_name(value) == name
        )
        return RuntimeRequestContext(server, reader, client, session_id, name)


class ResultRuntimeRequest(RuntimeRequest):
    async def apply(self, ctx: RuntimeRequestContext) -> None:
        await ctx.send({"result": await self.result(ctx)})

    @abstractmethod
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]: ...


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
        config_options = await agent.sessions.config.options(ctx.name)
        metadata = agent.sessions.metadata(ctx.name)
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
        return ctx.server.agent._comms.goals.input_delivery(
            ctx.name,
            include_history=self.include_history,
            awaiting_keys=ctx.server.agent.inputs.awaiting_input_keys(ctx.session_id),
        )


@dataclass(frozen=True, kw_only=True)
class DismissHistoricalInputsRuntimeRequest(ResultRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        result = ctx.server.agent._comms.goals.dismiss_historical_inputs(
            ctx.name, awaiting_keys=ctx.server.agent.inputs.awaiting_input_keys(ctx.session_id)
        )
        await ctx.server.agent.inputs.emit_input_delivery_changed(ctx.session_id)
        return result


@dataclass(frozen=True, kw_only=True)
class GoalHistoryRuntimeRequest(ResultRuntimeRequest):
    goal_id: str | None = None

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        history = ctx.server.agent._comms.goals.goal_history(ctx.name, goal_id=self.goal_id)
        return {"history": [row.to_wire() for row in history]}


class GoalSnapshotResultRuntimeRequest(ResultRuntimeRequest):
    @abstractmethod
    async def change(self, ctx: RuntimeRequestContext) -> None: ...

    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        await self.change(ctx)
        goal, execution = ctx.server.agent._comms.goals.goal_snapshot(ctx.name)
        return {
            "goal": goal.to_wire() if goal is not None else None,
            "goalExecution": asdict(execution) if execution is not None else None,
        }


@dataclass(frozen=True, kw_only=True)
class GoalSnapshotRuntimeRequest(GoalSnapshotResultRuntimeRequest):
    async def change(self, ctx: RuntimeRequestContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class GoalRevisionRuntimeRequest(ResultRuntimeRequest):
    goal_id: str
    expected_revision: int

    def precondition(self, ctx: RuntimeRequestContext) -> GoalPrecondition:
        """Bind every revisioned UI command to the same complete current goal.

        GoalAction rechecks this capture under its canonical wire lock before
        mutation; reading here never grants permission to a later owner.
        """
        goal = ctx.server.agent._comms.registry.require(ctx.name).goal
        if goal is None or goal.id != self.goal_id or goal.revision != self.expected_revision:
            raise ValueError("The goal changed; refresh its state before updating.")
        return GoalPrecondition(
            goal_id=self.goal_id,
            expected_goal=goal,
            expected_owner_pid=os.getpid(),
        )


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
        agent = ctx.server.agent
        agent._comms.goals.update_goal(
            ctx.name,
            EditGoalAction(text=self.text, expect=self.precondition(ctx)),
            actor=OwnerInvocable,
        )
        await agent.sessions.config.sync_thread(ctx.session_id)


@dataclass(frozen=True, kw_only=True)
class UpdateGoalRuntimeRequest(GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest):
    status: str | None = None

    async def change(self, ctx: RuntimeRequestContext) -> None:
        action = GoalAction.decode(self.status)
        if not issubclass(action, OwnerControlInvocable):
            raise ValueError("Goal updates support only active, paused, or clear.")
        agent = ctx.server.agent
        expect = self.precondition(ctx)
        try:
            agent._comms.goals.update_goal(
                ctx.name,
                action(expect=expect),
                actor=OwnerInvocable,
                owner_store=agent.turns.open_goal_store() if action.owner_grant else None,
            )
        finally:
            # Resume may reconcile a failed attempt to BLOCKED before refusing.
            # Publish that durable state even when the request returns an error.
            await agent.sessions.config.sync_thread(ctx.session_id)
        if action.schedules_goal:
            agent.turns.schedule_goal(ctx.session_id)


@dataclass(frozen=True, kw_only=True)
class RetryGoalRuntimeRequest(GoalRevisionRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        agent = ctx.server.agent
        expect = self.precondition(ctx)
        if agent.turns.pending_goal_origins.get(ctx.name) == self.goal_id:
            raise ValueError("Wait for the goal origin turn to finish.")
        goal = agent._comms.goals.update_goal(
            ctx.name,
            RetryGoalAction(expect=expect),
            actor=OwnerInvocable,
            owner_store=agent.turns.open_goal_store(),
        )
        assert goal is not None
        # The durable decision is immediate. Existing scheduler busy fences
        # defer its fresh launch until the unrelated turn finishes.
        agent.turns.schedule_goal(ctx.session_id)
        await agent.turns.sync_goal_execution(ctx.session_id, ctx.name)
        return {"goal": goal.to_wire()}


@dataclass(frozen=True, kw_only=True)
class SetGoalRuntimeRequest(GoalTextRuntimeRequest):
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]:
        agent = ctx.server.agent
        goal = agent._comms.goals.update_goal(
            ctx.name,
            SetGoalAction(text=self.text, expect=GoalPrecondition(expected_owner_pid=os.getpid())),
            actor=OwnerInvocable,
            owner_store=agent.turns.open_goal_store(),
        )
        assert goal is not None
        agent.turns.schedule_goal(ctx.session_id)
        return {"goal": goal.to_wire()}
