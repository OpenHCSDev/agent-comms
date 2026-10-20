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


def test_native_new_order_refuses_without_writing(commander):
    comms, _ = commander
    with pytest.raises(ValueError, match='never checks out'):
        create(commander, 'P1', command='git checkout main')
    assert comms.goals.todos.for_commander(comms.registry.require('lead').incarnation) == ()


def test_saved_order_can_be_read_sent_about_and_explicitly_corrected(commander):
    comms, caller = commander
    current = create(commander, 'P1')
    legacy_check = OrderCheck(str(comms.root), 'git checkout main && printf 0')
    historical = replace(current, order=replace(current.order, check=legacy_check))
    store = comms.goals.todos
    # Seed a historical row, not a new authored order through the public write API.
    with store.writing() as db:
        historical.upsert(db)
    assert store.get('P1') == historical
    assert store.for_commander(comms.registry.require('lead').incarnation) == (historical,)
    CommsSendTool(sender='worker', target='lead', body='The recorded check selects a revision.').apply(
        comms, ThreadCaller('worker'))
    with pytest.raises(ValueError, match='never checks out'):
        store.apply('P1', AssignTodo(historical.revision, historical.assignment,
            historical.deadline, order(comms.root, 'replacement', command=legacy_check.command),
            historical.goal))
    with pytest.raises(ValueError, match='never checks out'):
        CommsOrderCheckTool(problem='P1', check=legacy_check,
            why='The command must not change the current revision.').apply(comms, caller)
    assert store.get('P1') == historical
    safe = OrderCheck(str(comms.root), 'printf 0')
    CommsOrderCheckTool(problem='P1', check=safe,
        why='The command must not change the current revision.').apply(comms, caller)
    corrected = store.get('P1')
    assert corrected.order.id == historical.order.id
    assert corrected.revision == historical.revision + 1
    assert corrected.order.check == safe
    assert corrected.order.check_corrections[-1].previous == legacy_check
