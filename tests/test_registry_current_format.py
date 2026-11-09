"""Registry persistence retains identity and goal behavior, never synthesizes authority."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.goal_states import (
    BlockedGoal,
    GoalState,
)
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile


def test_goal_family_survives_registry_and_history_reopen(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    for name in GoalState.names():
        state_type = GoalState.member_named(name)
        state = (
            BlockedGoal(block_reason="Waiting for the fixture dependency")
            if state_type is BlockedGoal else state_type()
        )
        goal_id = GoalId.mint()
        goal = Goal("Keep the objective", goal_id, published_seq=0, state=state)
        owner = NativeThread(name, frozenset(), Checkout(tmp_path), goal=goal)
        registry.register(owner)
        assert FieldCodec.decode(Goal, goal.to_wire()) == goal
        reopened = Registration(registry.store.path)
        assert reopened.require(name).goal == goal
        assert reopened.require(name).incarnation == owner.incarnation
        assert reopened.goal_history(name)[-1].after == goal
    program = """
import sys
from pathlib import Path
from agent_comms.comms import wire
from agent_comms.planning.goal_states import GoalState
registry = wire(Path(sys.argv[1]).parent).registry
for name in GoalState.names():
    goal = registry.require(name).goal
    assert goal.state.declared_name == name
    assert registry.goal_history(name)[-1].after == goal
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(registry.store.path)],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src")),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_incomplete_saved_identity_cannot_authorize_an_owner(tmp_path):
    path = RegistryFile.path_in(tmp_path)
    registry = Registration(path)
    registry.register(NativeThread('owner', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    raw = RegistryStore(path).stored()
    for field in ("owners", "claims"):
        incomplete = {**raw, field: {**raw[field], "generations": {}}}
        RegistryStore(path).rewrite_stored(lambda _, stored=incomplete: stored)
        with pytest.raises(RelationViolationError):
            Registration(path).live_owner_with_claim("owner")
    raw["threads"]["owner"].pop("created_at")
    RegistryStore(path).rewrite_stored(lambda _, stored=raw: stored)
    with pytest.raises(RelationViolationError) as rejected:
        Registration(path).require("owner")
    assert isinstance(rejected.value.__cause__, ValueError)


def test_retained_colliding_creation_dates_remain_readable(tmp_path):
    path = RegistryFile.path_in(tmp_path)
    registry = Registration(path)
    registry.register(NativeThread('one', frozenset(), Checkout(tmp_path)))
    registry.register(NativeThread('two', frozenset(), Checkout(tmp_path)))
    raw = RegistryStore(path).stored()
    raw["threads"]["two"]["created_at"] = raw["threads"]["one"]["created_at"]
    RegistryStore(path).rewrite_stored(lambda _, stored=raw: stored)
    reopened = Registration(path)
    assert reopened.require("one").created_at == reopened.require("two").created_at
