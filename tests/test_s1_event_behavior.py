"""Current S1 consumer effects and fenced settlement, with real durable owners."""

import asyncio
import os
from abc import abstractmethod
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import pytest
from acp import RequestError

import agent_comms.agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.agent_backend import PlacedCompaction
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
from agent_comms.goal_waits import NoGoalWait, RecordedGoalWait
from agent_comms.messages import InfoKind, Message, NoticeRole
from agent_comms.owned_send import OwnedSend
from agent_comms.owned_turn import OwnedTurn
from agent_comms.pi_counts import PiCount
from agent_comms.thread_identity import TurnId
from agent_comms.threads import Thread
from agent_comms.transcript_updates import TurnTranscriptUpdate
from bus_history import read_history
from stored_inputs import stored_inputs

pytestmark = pytest.mark.usefixtures("fake_native_catalog")


class EffectCase(DeclaredFamily, affix="Case"):
    """Case-owned streams/expectations, discovered without a hand-maintained roster."""

    @abstractmethod
    def body(self): ...

    successful = True

    async def assert_effect(self, execution, progress):
        assert progress.result is not None and progress.result.ok is self.successful


class NormalCase(EffectCase):
    def body(self):
        return (ae.Chunk("answer"),)


class ToolCase(EffectCase):
    def body(self):
        return (
            ae.ToolStart("tool", "read", "Read source", {"path": "file"}),
            ae.ToolProgress("tool", "read", "reading"),
            ae.ToolEnd("tool", "read", True, "read"),
            ae.Chunk("answer"),
        )

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        assert progress.goals.productive_tool == self.body()[2]


class CompactionCase(EffectCase):
    def body(self):
        return (
            ae.CompactionStart(),
            ae.ToolStart("tool", "read", "Read after summary"),
            ae.CompactionEnd(summary="summary"),
            ae.AgentInfo(model="model", context_size=PiCount.from_wire(1000)),
            ae.ContextUsageObservation(context_used=PiCount.from_wire(20)),
            ae.Chunk("answer"),
        )

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        info = execution.runner.comms.agents.agent_info_of(execution.thread_name)
        assert info.context_used == PiCount.from_wire(20) and info.context_size == PiCount.from_wire(1000)


class CompactionAbortCase(CompactionCase):
    def body(self):
        return (
            ae.CompactionStart(),
            ae.CompactionEnd(aborted=True),
            ae.AgentInfo(model="model", context_size=PiCount.from_wire(1000)),
            ae.ContextUsageObservation(context_used=PiCount.from_wire(20)),
            ae.Chunk("answer"),
        )


class ContextCase(EffectCase):
    def body(self):
        return (
            ae.AgentInfo(model="model", context_size=PiCount.from_wire(1000)),
            ae.ContextUsageObservation(context_used=PiCount.from_wire(20)),
            ae.ContextInfo(context_used=PiCount.from_wire(None), context_size=PiCount.from_wire(1000)),
            ae.ContextInfo(context_used=PiCount.from_wire(35), context_size=PiCount.from_wire(1000)),
            ae.Chunk("answer"),
        )

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        info = execution.runner.comms.agents.agent_info_of(execution.thread_name)
        assert info.model == "model"
        assert (info.context_used, info.context_size) == (35, 1000)


class SettingsCase(EffectCase):
    def body(self):
        return (
            ae.ModelChanged("same", True),
            ae.ThinkingChanged("same", False, "level unavailable"),
            ae.ModelChanged("same", False, "late"),
            ae.Chunk("answer"),
        )


class DiagnosticFailureCase(EffectCase):
    successful = False

    def body(self):
        return (
            ae.Chunk("provisional"),
            ae.Error("provider failed"),
        )


class ReasonFailureCase(DiagnosticFailureCase):
    def body(self):
        return (
            ae.Chunk("provisional"),
            ae.Error("provider failed", "backend_failed"),
        )


class InterruptedCase(EffectCase):
    def body(self):
        return (ae.Chunk("stale"), ae.SteeringInterrupted(), ae.Chunk("answer"))

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        assert progress.reply_parts == ["answer"]


@pytest.fixture
async def owner_turn(comms, tmp_path, fake_native_catalog):
    owner = CommsAgent(
        comms,
        agent_bin="unused",
    )
    session = await owner.new_session(cwd=str(tmp_path), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    origin = comms.messaging.send_user_message(name, "work", worktree=str(tmp_path))
    execution = OwnedTurn(
        owner.turns, session.session_id, name, "work", origins=(origin,)
    )
    try:
        async with AsyncExitStack() as resources:
            async with AsyncExitStack() as permits:
                assert await execution.acquire(resources, permits)
                yield execution, execution.progress
    finally:
        await owner.shutdown()


async def start_owned_input(execution, progress):
    claim_scope = OwnedSend(
        comms=execution.runner.comms,
        owner_name=execution.thread.name,
        inputs=execution.runner.inputs,
        goal_store=execution.runner.comms.goals.open_goal_store(),
        session_id=execution.session_id,
        thread=execution.thread,
        turn=TurnId(execution.turn_id),
        claim_scope=execution.turn_lease.claimed_generation,
        original=execution.original,
        goal_permit=execution.goal_permit,
    )
    native_id = uuid4().hex
    with claim_scope(None, native_id, execution.original.prompt):
        pass
    assert claim_scope.native_start(None, native_id, execution.original.prompt)
    await progress.consume(ae.OriginalInputStarted())


def waiting_owner(execution):
    comms = execution.runner.comms
    comms.registry.declare(
        comms.registry.new_thread('waiting', frozenset(), execution.thread.worktree, process_identity=ProcessIdentity.capture(os.getpid()))
    )
    goal = comms.goals.update_goal("waiting", SetGoalAction(text="Review dependency"))
    comms.goals.update_goal(
        "waiting",
        StandbyGoalAction(
            expect=GoalPrecondition(goal_id=goal.id), wait_for=(execution.thread_name,)
        ),
    )
    return goal


