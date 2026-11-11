"""Own live publications advance authority; external changes and saved data do not."""
from dataclasses import replace

import pytest

from agent_comms.execution.channel_coding_tools import OrdinaryGoalRule
from agent_comms.execution.turn_goal_permission import (
    OwnerGoalPermission,
    TurnGoalPermission,
)
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.pi_payloads import ProvidedToolResult
from agent_comms.planning.goal_states import (
    BlockedGoal,
    CompletedGoal,
    PausedGoal,
)
from agent_comms.planning.goals import (
    Goal,
    NoGoal,
    ProblemGoalOrder,
)
from agent_comms.transport.native_tools import CoordinationCall, ReadTool
from agent_comms.transport.tool_denial import SelectedToolDenied
from agent_comms.transport.tools.goals import GoalSet


def test_goal_publication_is_exact_and_coding_output_cannot_author_it():
    own_goal_id = GoalId.mint()
    another_goal_id = GoalId.mint()
    original = NoGoal()
    goal = Goal('the actual publication', own_goal_id, published_seq=0)
    permission = TurnGoalPermission(OwnerGoalPermission(original.authority))
    result = ProvidedToolResult.from_wire({'content': [], 'details':
        FieldCodec.encode(GoalSet(goal, 'continue in this turn'))})
    # Neither decoding a saved result nor coding output grants authority.
    assert permission.allows(original) and not permission.allows(goal)
    ReadTool(call_id='coding', path='x').published_goal(result, permission)
    assert not permission.allows(goal)
    CoordinationCall(call_id='own', declared_name='comms_set_goal', arguments={}).published_goal(result, permission)
    assert permission.allows(goal)
    assert not permission.allows(replace(goal, revision=goal.revision + 1, state=PausedGoal()))
    assert not permission.allows(replace(goal, id=another_goal_id))


def test_order_revision_keeps_the_granted_authority():
    """An assigned or replaced problem order revises the goal; the running turn keeps working."""
    order_goal_id = GoalId.mint()
    granted = Goal(ProblemGoalOrder.text, order_goal_id, published_seq=0, order=ProblemGoalOrder())
    permission = TurnGoalPermission(OwnerGoalPermission(granted.authority))
    ordered = replace(granted, revision=granted.revision + 1, progress="")
    OrdinaryGoalRule.require(permission, ordered)
    assert FieldCodec.comparison_digest(OwnerGoalPermission(ordered.authority)) == (
        FieldCodec.comparison_digest(permission.captured)
    )
    assert FieldCodec.comparison_digest(OwnerGoalPermission(NoGoal().authority)) != (
        FieldCodec.comparison_digest(permission.captured)
    )
    assert not permission.allows(replace(ordered, state=BlockedGoal("stopped by its owner")))
    assert not permission.allows(replace(ordered, state=PausedGoal()))
    OrdinaryGoalRule.require(permission, NoGoal())


def test_a_dead_permission_names_both_authorities_it_compared():
    """Replacing a goal still refuses tools captured under a different goal."""
    completed = Goal('the completed goal', GoalId.mint(), published_seq=0, state=CompletedGoal())
    current = Goal('the replacement goal', GoalId.mint(), published_seq=0)
    permission = TurnGoalPermission(OwnerGoalPermission(completed.authority))
    with pytest.raises(SelectedToolDenied) as denial:
        OrdinaryGoalRule.require(permission, current)
    said = str(denial.value)
    assert str(completed.id) in said
    assert 'in state completed' in said
    assert f'the goal is now goal {current.id}' in said
