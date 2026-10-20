"""Own live publications advance authority; external changes and saved data do not."""
import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import OwnerInvocable, PausedGoalAction
from agent_comms.goal_states import PausedGoal
from agent_comms.goals import Goal, NoGoal
from agent_comms.native_tools import CoordinationCall, NativeToolDeclaration, ReadTool
from agent_comms.native_runtime_input import CurrentNativeCursor
from delivery_owner_fixture import reply_turn_finished
from agent_comms.pi_payloads import ProvidedToolResult
from agent_comms.tools import GoalSet
from agent_comms.turn_goal_permission import OwnerGoalPermission, TurnGoalPermission


def test_goal_publication_is_exact_and_coding_output_cannot_author_it():
    original = NoGoal()
    goal = Goal('the actual publication', 'own-goal', published_seq=0)
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
    assert not permission.allows(replace(goal, id='another-commanders-goal'))
