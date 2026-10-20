"""Saved check commands are data; revision policy is enforced when writing new checks."""
from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.goals import OrderCheck
from agent_comms.tool_caller import ThreadCaller
from agent_comms.todos import AssignTodo
from agent_comms.tools import CommsOrderCheckTool, CommsSendTool
from test_commander_problem_orders import create, order


@pytest.mark.parametrize('command', ['git checkout main', 'git -C /repo switch work',
                                   'true && git checkout HEAD'])
def test_saved_checks_decode_but_new_checks_refuse_revision_selection(command):
    saved = OrderCheck('.', command)
    assert FieldCodec.decode(OrderCheck, FieldCodec.encode(saved)) == saved
    with pytest.raises(ValueError, match='never checks out'):
        saved.require_writable()
    with pytest.raises(ValueError, match='never checks out'):
        OrderCheck.parse(f'. :: {command} -> 0')


