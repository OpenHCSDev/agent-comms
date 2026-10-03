"""Original begin/attachment ownership with real registry and callback resources."""

from contextlib import AsyncExitStack
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.agent_events import AgentInfo
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import StaleFence
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.owned_turn import OwnedTurn
from agent_comms.threads import Thread
from agent_comms.turn_phase import PreparingPhase


@pytest.mark.asyncio
async def test_begin_callback_and_native_attachment_share_original_owner(tmp_path):
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
            saved = str(tmp_path / "observed-native.jsonl")
            await turn.progress.before_agent_info(AgentInfo(session_file=saved))
            assert turn.registry_owner is not initial
            assert turn.thread.session_file == saved
            assert turn.progress.thread is turn.thread
            assert turn.progress.turn_lease == initial.turn_lease
            assert comms.registry.require("owner").turn_lease == initial.turn_lease
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
