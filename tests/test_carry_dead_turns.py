"""Superseded turn files are deleted, and the carry names each one it deleted."""

import os
import shutil

from agent_comms.child_process import ProcessIdentity
from cutover.carry_dead_turns import DeadTurns
from goal_owner_fixture import canonical_goal_wire
from agent_comms.threads import Thread


def test_deleted_turn_files_are_named_and_the_current_one_kept(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    comms.registry.declare(Thread(
        "carol", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    comms.agents.begin_turn("carol", "carol-turn")
    current = comms.registry.require("carol").require_turn_lease().record.path
    incarnation, generation = current.stem.rsplit("-", 1)
    dead = current.with_name(f"{incarnation}-{int(generation) + 100}.json")
    shutil.copyfile(current, dead)
    step = DeadTurns(comms.root, comms.root, tmp_path / "spill")
    assert step.carry(apply=False).rewritten == ()
    carried = step.carry(apply=True)
    assert carried.rewritten == (dead.relative_to(comms.root),)
    assert current.exists() and not dead.exists()
    assert step.carry(apply=False).changed == 0
