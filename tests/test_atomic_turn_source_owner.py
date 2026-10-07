"""Original begin/attachment ownership with real registry and callback resources."""

from contextlib import AsyncExitStack
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.agent_events import AgentInfo
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import StaleFence
from agent_comms.goal_actions import SetGoalAction
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.owned_turn import OwnedTurn
from agent_comms.threads import Thread
from agent_comms.turn_phase import PreparingPhase


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_changed", [False, True])
async def test_begin_callback_and_native_attachment_share_original_owner(tmp_path, goal_changed):
    comms = Comms(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid())))
    agent = CommsAgent(comms, auto_wake=False)
    turn = OwnedTurn(agent.turns, "owner-session", "owner", "new original input")
    async with AsyncExitStack() as resources:
        async with AsyncExitStack() as permits:
            assert comms.registry.require("owner").turn_lease is None
            assert await turn.acquire(resources, permits)
            initial = turn.registry_owner
            assert isinstance(initial, RegistryOwner)
            assert initial.turn_lease == comms.registry.require("owner").turn_lease
            assert turn.progress.turn is turn
            comms.agents.transition_turn(turn.turn_lease, PreparingPhase("changed detail"))
            if goal_changed:
                comms.goals.update_goal("owner", SetGoalAction(text="A distinct goal during this turn"))
                # An informational source observation grants no new input.
                # The original input/selected admission still refuses this goal.
                with pytest.raises(StaleFence, match="registry_goal"):
                    initial.require_snapshot(comms.registry.snapshot(), "Original input changed")
            current = comms.registry.require("owner")
            status = comms.registry.status("owner")
            saved = str(tmp_path / "observed-native.jsonl")
            await turn.progress.before_agent_info(AgentInfo(session_file=saved))
            assert turn.registry_owner is not initial
            assert turn.thread.session_file == saved
            assert turn.progress.thread is turn.thread
            assert turn.progress.turn_lease == initial.turn_lease
            assert comms.registry.require("owner").turn_lease == initial.turn_lease
            assert turn.thread.goal == current.goal
            assert turn.thread.turn_state == current.turn_state
            assert comms.registry.status("owner") == status
            assert not {"thread", "turn_lease", "routing", "original", "checkpoint"} & vars(turn.progress).keys()
    assert comms.registry.require("owner").turn_lease is None


@pytest.mark.parametrize("replacement", ["same-turn-id", "new-admission"])
def test_source_attachment_refuses_replaced_original_owner(tmp_path, replacement):
    comms = Comms(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.agents.begin_turn("owner", "same-id")
    assert comms.agents.finish_turn(original.turn_lease)
    if replacement == "new-admission":
        comms.registry.register(comms.registry.require("owner"), new_owner=True)
    current = comms.agents.begin_turn("owner", "same-id")
    before = comms.registry.store.path.read_bytes()
    with pytest.raises(StaleFence):
        comms.registry.attach_native_session(original, str(tmp_path / "wrong.jsonl"))
    assert comms.registry.store.path.read_bytes() == before
    assert comms.agents.finish_turn(current.turn_lease)


def test_native_source_publication_retains_captured_configuration(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()), model="test/old"))
    original = comms.agents.begin_turn("owner", "configuration-source-check")
    comms.threads.set_thread_model("owner", "test/next")
    with pytest.raises(StaleFence, match="registry_model"):
        original.require_snapshot(comms.registry.snapshot(), "Fresh input changed")
    selected = comms.registry.attach_native_session(original, str(tmp_path / "saved.jsonl"))
    assert selected.thread.model == "test/old"
    assert selected.thread.thinking_level == original.thread.thinking_level
    assert selected.turn_lease == original.turn_lease
    assert comms.registry.require("owner").model == "test/next"
    assert comms.agents.finish_turn(original.turn_lease)
