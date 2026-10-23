"""A referenced turn file's bare owner PID becomes its thread's process identity."""

import json
import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from cutover.carry_turn_owner import TurnOwner
from goal_owner_fixture import canonical_goal_wire


def predecessor_turn(tmp_path, pid):
    comms = canonical_goal_wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        "carol", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    comms.agents.begin_turn("carol", "carol-turn")
    path = comms.registry.require("carol").require_turn_lease().record.path
    stored = json.loads(path.read_text())
    stored["owner_pid"] = pid
    del stored["owner"]
    path.write_text(json.dumps(stored))
    return comms, TurnOwner(comms.root, comms.root, tmp_path / "spill")


def test_carry_records_the_leased_identity_once(tmp_path):
    comms, step = predecessor_turn(tmp_path, os.getpid())
    assert step.carry(apply=False).changed == 1
    assert step.carry(apply=True).changed == 1
    turn = comms.registry.require("carol").require_turn_lease().record.read()
    assert turn.owner == ProcessIdentity.capture(os.getpid())
    assert step.carry(apply=False).changed == 0


def test_carry_refuses_a_pid_its_thread_never_recorded(tmp_path):
    _, step = predecessor_turn(tmp_path, os.getpid() + 1)
    with pytest.raises(RelationViolationError):
        step.carry(apply=False)
