"""Registry persistence retains identity and goal behavior, never synthesizes authority."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_states import BlockedGoal, GoalState
from agent_comms.goals import Goal
from agent_comms.registration import Registration
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import Thread


def test_goal_family_survives_registry_and_history_reopen(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    for name in GoalState.names():
        state_type = GoalState.decode(name)
        state = (
            BlockedGoal(block_reason="Waiting for the fixture dependency")
            if state_type is BlockedGoal else state_type()
        )
        goal = Goal("Keep the objective", name, state=state)
        owner = Thread(name, frozenset(), str(tmp_path), goal=goal)
        registry.register(owner)
        assert FieldCodec.decode(Goal, goal.to_wire()) == goal
        reopened = Registration(registry.store.path)
        assert reopened.require(name).goal == goal
        assert reopened.require(name).incarnation == owner.incarnation
        assert reopened.goal_history(name)[-1].after == goal
    program = """
import sys
from pathlib import Path
from agent_comms.registration import Registration
from agent_comms.goal_states import GoalState
registry = Registration(Path(sys.argv[1]))
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
    path = tmp_path / "registry.json"
    registry = Registration(path)
    registry.register(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    raw = json.loads(path.read_text())
    for field in ("owners", "admissions"):
        incomplete = dict(raw)
        incomplete.pop(field)
        path.write_text(json.dumps(incomplete))
        with pytest.raises(RelationViolationError, match="generation"):
            Registration(path).live_owner_with_generation("owner")
    raw["threads"]["owner"].pop("created_at")
    path.write_text(json.dumps(raw))
    with pytest.raises(RelationViolationError) as rejected:
        Registration(path).require("owner")
    assert isinstance(rejected.value.__cause__, ValueError)
    assert "Missing required fields for Thread" in str(rejected.value.__cause__)


def test_retained_colliding_creation_dates_remain_readable(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registration(path)
    registry.register(Thread("one", frozenset(), str(tmp_path)))
    registry.register(Thread("two", frozenset(), str(tmp_path)))
    raw = json.loads(path.read_text())
    raw["threads"]["two"]["created_at"] = raw["threads"]["one"]["created_at"]
    path.write_text(json.dumps(raw))
    reopened = Registration(path)
    assert reopened.require("one").created_at == reopened.require("two").created_at
