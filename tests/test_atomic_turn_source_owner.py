"""Original begin/attachment ownership with real registry and callback resources."""

from agent_comms.turn_input_source import NoTurnAcceptance


from contextlib import AsyncExitStack
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.agent_events import AgentInfo
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.coordination_errors import StaleFence
from agent_comms.field_codec import TurnId
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.goal_actions import SetGoalAction
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.owned_turn import OwnedTurn
from agent_comms.turn_phase import PreparingPhase
from agent_comms.threads import ChosenModel, NextTurnConfiguration
from agent_comms.thread_model import DeclaredModel
from agent_comms.turn_lease import NoTurnLease
from agent_comms.threads import Thread


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_changed", [False, True])
async def test_begin_callback_and_native_attachment_share_original_owner(tmp_path, goal_changed):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    agent = CommsAgent(comms, auto_wake=False)
    turn = OwnedTurn(
        agent.turns, "owner-session", "owner", "new original input", acceptance=NoTurnAcceptance(),
    )
    async with AsyncExitStack() as resources:
        async with AsyncExitStack() as permits:
            assert comms.registry.require("owner").turn_lease == NoTurnLease()
            assert await turn.acquire(resources, permits)
            initial = turn.registry_owner
            assert isinstance(initial, RegistryOwner)
            assert initial.turn_lease == comms.registry.require("owner").turn_lease
            assert turn.progress.turn is turn
            comms.agents.transition_turn(turn.turn_lease, PreparingPhase("changed detail"))
            if goal_changed:
                comms.goals.update_goal("owner", SetGoalAction(text="A distinct goal during this turn"))
                # An informational source observation grants no new input.
                # The original input/selected claim_scope still refuses this goal.
                with pytest.raises(StaleFence):
                    initial.require_snapshot(comms.registry.snapshot(), "Original input changed")
            current = comms.registry.require("owner")
            status = comms.registry.status("owner")
            native_file = tmp_path / "observed-native.jsonl"
            native_file.write_text('{"type":"session","version":3,"id":"observed-native"}\n')
            saved = str(native_file)
            await turn.progress.before_native_metadata(AgentInfo(session_file=saved))
            assert turn.registry_owner is not initial
            assert turn.thread.session_file == RegistrySessionLocation.from_path(saved, comms.root)
            assert turn.progress.thread is turn.thread
            assert turn.progress.turn_lease == initial.turn_lease
            assert comms.registry.require("owner").turn_lease == initial.turn_lease
            assert turn.thread.goal == current.goal
            assert turn.thread.turn_state == current.turn_state
            assert comms.registry.status("owner") == status
            assert not {"thread", "turn_lease", "routing", "original", "checkpoint"} & vars(turn.progress).keys()
    assert comms.registry.require("owner").turn_lease == NoTurnLease()


@pytest.mark.parametrize("replacement", ["same-turn-id", "new-claim_scope"])
def test_source_attachment_refuses_replaced_original_owner(tmp_path, replacement):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.agents.begin_turn("owner", TurnId('058be655fae2a4c37a21d2c091ba7661'))
    assert comms.agents.finish_turn(original.turn_lease)
    if replacement == "new-claim_scope":
        comms.registry.register(comms.registry.require("owner"), new_owner=True)
    current = comms.agents.begin_turn("owner", TurnId('058be655fae2a4c37a21d2c091ba7661'))
    before = comms.registry.store.path.read_bytes()
    with pytest.raises(StaleFence):
        comms.registry.attach_native_session(original, str(tmp_path / "wrong.jsonl"))
    assert comms.registry.store.path.read_bytes() == before
    assert comms.agents.finish_turn(current.turn_lease)


def test_native_source_publication_retains_original_claim_across_rename(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread(
        'owner', frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    original = comms.agents.begin_turn('owner', TurnId('9848d76b0bc0fadfe6c6463059c2c99b'))
    comms.registry.rename('owner', 'renamed')
    snapshot = comms.registry.snapshot()
    assert original.require_source_snapshot(snapshot).name == 'renamed'
    assert RegistryOwner.capture(snapshot, 'owner', 'Original claim lost').claimed_generation == original.claimed_generation
    selected = comms.registry.attach_native_session(original, str(tmp_path / 'saved.jsonl'))
    assert selected.thread.name == 'renamed'
    assert comms.agents.finish_turn(original.turn_lease)
    comms.registry.register(comms.registry.require('renamed'), new_owner=True)
    with pytest.raises(StaleFence):
        original.require_snapshot(comms.registry.snapshot(), 'Replaced owner refused')


def test_native_source_publication_retains_captured_configuration(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model=DeclaredModel('test/old')))
    original = comms.agents.begin_turn("owner", TurnId('353fe4ec44fe236b973ecec2489ac459'))
    comms.threads.set_thread_configuration(
            comms.registry.require('owner'), NextTurnConfiguration(model=ChosenModel(DeclaredModel('test/next'))))
    original.require_snapshot(comms.registry.snapshot(), "The claimed turn keeps its configuration")
    selected = comms.registry.attach_native_session(original, str(tmp_path / "saved.jsonl"))
    assert selected.thread.model == DeclaredModel("test/old")
    assert selected.thread.thinking_level == original.thread.thinking_level
    assert selected.turn_lease == original.turn_lease
    assert comms.registry.require("owner").model == DeclaredModel("test/next")
    assert comms.agents.finish_turn(original.turn_lease)
