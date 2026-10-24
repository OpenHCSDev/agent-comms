"""Own live publications advance authority; external changes and saved data do not."""
from dataclasses import replace


from agent_comms.field_codec import FieldCodec, GoalId
from agent_comms.goal_states import BlockedGoal, PausedGoal
from agent_comms.goals import Goal, NoGoal, ProblemGoalOrder
from agent_comms.native_tools import CoordinationCall, ReadTool
from agent_comms.pi_payloads import ProvidedToolResult
from agent_comms.tools import GoalSet
from agent_comms.turn_goal_permission import OwnerGoalPermission, TurnGoalPermission


def test_goal_publication_is_exact_and_coding_output_cannot_author_it():
    own_goal_id = GoalId.mint()
    another_goal_id = GoalId.mint()
    original = NoGoal()
    goal = Goal('the actual publication', own_goal_id, published_seq=0)
    permission = TurnGoalPermission(OwnerGoalPermission(original))
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
    unit_goal_id = GoalId.mint()
    granted = Goal(ProblemGoalOrder.text, unit_goal_id, published_seq=0, order=ProblemGoalOrder())
    permission = TurnGoalPermission(OwnerGoalPermission(granted))
    ordered = replace(granted, revision=granted.revision + 1, progress="")
    permission.require_current(ordered)
    assert not permission.allows(replace(ordered, state=BlockedGoal("stopped by its owner")))
    assert not permission.allows(replace(ordered, state=PausedGoal()))
    assert not permission.allows(NoGoal())
