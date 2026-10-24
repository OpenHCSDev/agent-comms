"""Named ordinary checks on a production-created owner/turn, plus real flock exclusion."""

from dataclasses import replace

import pytest


import agent_comms.ordinary_turn_rules as rules
from agent_comms.field_codec import GoalId
from agent_comms.reservation_rules import ReservationViolationError
from agent_comms.goal_waits import NoGoalWait, RecordedGoalWait
from agent_comms.threads import Thread


def test_routed_dependency_source_uses_existing_goal_and_wait_authorities(tmp_path):
    goal_id = GoalId.mint()
    from agent_comms.channel_input_batch import SingleInputBatch
    from agent_comms.goal_presentation import GoalWaitTarget
    from agent_comms.goals import Goal
    from agent_comms.turn_goal_permission import ContinuationGoalPermission, TurnGoalPermission
    from agent_comms.turn_input_source import (
        CapturedInputDependency,
        DependencyOriginalInput,
        NoInputDependency,
        NoTurnAcceptance,
        RoutedOriginalInput,
    )

    from goal_owner_fixture import canonical_goal_wire

    comms = canonical_goal_wire(tmp_path / "wire")
    comms.registry.declare(Thread(name='owner', tags=frozenset(), worktree=str(tmp_path)))
    peer = Thread(name='peer', tags=frozenset(), worktree=str(tmp_path))
    comms.registry.declare(peer)
    peer = comms.registry.require("peer")
    goal = Goal("Wait for peer", goal_id, published_seq=0)
    message = comms.messaging.send("peer", "owner", "Result").message
    wait = RecordedGoalWait(
        goal_id=goal.id,
        wait_id="wait",
        revision=0,
        after_seq=0,
        targets=(GoalWaitTarget(peer.incarnation.require_recorded()),),
        owner=comms.registry.require("owner").incarnation.require_recorded(),
        target_turn_generations=(None,),
        report_turn_id=None,
        report_turn_generation=None,
    )
    common = dict(
        acceptance=NoTurnAcceptance(),
        goal_permission=TurnGoalPermission(ContinuationGoalPermission(goal)),
        prompt="Result",
        original_display="Result",
        origins=(message,),
        batch=SingleInputBatch(()),
    )
    dependency = DependencyOriginalInput(
        **common, dependency=CapturedInputDependency(wait.wait_id)
    )
    routed = RoutedOriginalInput(**common, dependency=NoInputDependency())
    rules.OrdinaryContextCheck(
        source=dependency, owner=comms.registry.require("owner"), goal=goal, wait=wait, comms=comms
    ).require_valid()
    # A keyless routed fixture has no captured input key to refuse on goal authority.
    rules.OrdinaryContextCheck(
        source=routed, owner=comms.registry.require("owner"), goal=goal,
        wait=NoGoalWait(), comms=comms
    ).require_valid()
    for source, current_wait, expected in (
        (dependency, NoGoalWait(), rules.OrdinaryDependencyRule),
        (dependency, replace(wait, wait_id="replacement"), rules.OrdinaryDependencyRule),
        (dependency, replace(wait, after_seq=message.seq), rules.OrdinaryWaitRule),
    ):
        with pytest.raises(ReservationViolationError) as error:
            rules.OrdinaryContextCheck(
                source=source, owner=comms.registry.require("owner"), goal=goal,
                wait=current_wait, comms=comms
            ).require_valid()
        assert type(error.value.rule) is expected
