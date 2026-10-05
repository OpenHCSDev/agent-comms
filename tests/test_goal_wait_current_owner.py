"""Wait graph traversal borrows the original goal and incarnation authority."""

from dataclasses import replace

import pytest

from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import GoalWait, GoalWaits
from agent_comms.threads import Thread
from goal_owner_fixture import canonical_goal_wire


@pytest.mark.parametrize("change", ["same", "later_goal", "future_wait", "replacement", "foreign_goal"])
def test_graph_uses_only_wait_for_original_owner_goal(tmp_path, change):
    comms = canonical_goal_wire(tmp_path / "wire")
    for name in ("owner", "peer", "leaf"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
        comms.goals.update_goal(name, SetGoalAction(text=f"Work for {name}"))
    peer = comms.registry.require("peer")
    leaf = comms.registry.require("leaf")
    goal = peer.active_goal
    wait = GoalWait(
        goal.id, "original-wait", goal.revision, 0,
        (GoalWaitTarget(leaf.name, leaf.created_at),), peer.created_at,
        (None,), None, None,
    )
    if change == "later_goal":
        comms.registry.register(replace(peer, goal=replace(goal, revision=goal.revision + 1)))
    elif change == "future_wait":
        wait = replace(wait, revision=goal.revision + 1)
    elif change == "replacement":
        wait = replace(wait, owner_created_at=peer.created_at + 1)
    elif change == "foreign_goal":
        wait = replace(wait, goal_id="different-goal")
    # The foreign-goal case models a mismatched persisted lookup key. Its row
    # must not become the current goal's dependency graph merely through lookup.
    group = GoalWaits.closed_wait_group(
        "owner", (GoalWaitTarget(peer.name, peer.created_at),),
        {goal.id: wait}, comms.registry.snapshot(),
    )
    # The graph reports waiting owners, not terminal leaves without a wait.
    assert group == (("owner", "peer") if change in {"same", "later_goal"}
                     else ("owner",))
