"""Recurring problems must replace their duplicated fact with a declaration."""
import json
from pathlib import Path

import pytest

from agent_comms.commander.order import OrderVerb
from agent_comms.todos import Slop, TodoError
from test_commander_problem_orders import order_body


def test_second_problem_on_one_owner_needs_its_declaration(commander, commander_script, monkeypatch):
    comms, _ = commander
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    body = Path(order_body(repo))
    document = json.loads(body.read_text())
    # Only the catalog resource is controlled; parsing, ordering and storage are real.
    monkeypatch.setattr(Slop, 'patterns', staticmethod(lambda: frozenset({'IDEN-3'})))
    where = f'{__file__}:1'
    citation = dict(pattern='IDEN-3', fact='which declaration owns the repeated fact',
                    at=where, derive='declaration',
                    by=f'{Path(__file__).parents[1] / "src/agent_comms/todos.py"}:1')
    body.write_text(json.dumps(document))

    def problem():
        OrderVerb.parse(['problem', str(body), '--to', 'worker', '--due', '23:59',
                         '--where', where], 'order').apply(script)

    problem()
    before = script.comms.goals.todos.history_for_commander(script.incarnation)
    with pytest.raises(TodoError) as refused:
        problem()
    text = str(refused.value)
    assert 'P1:' in text and before[0].order.checked().owner.to_wire() in text
    assert 's old' in text and 'the same file broke again' in text
    assert 'the declaration that replaces the copies' in text
    assert script.comms.goals.todos.history_for_commander(script.incarnation) == before

    document['slop'] = [dict(citation, derive='delete-copy')]
    body.write_text(json.dumps(document))
    with pytest.raises(TodoError, match='P1:'):
        problem()
    document['slop'] = [dict(citation, by=where)]
    body.write_text(json.dumps(document))
    with pytest.raises(TodoError, match='P1:'):
        problem()
    document['slop'] = [citation]
    body.write_text(json.dumps(document))
    problem()
    assert len(comms.goals.todos.history_for_commander(script.incarnation)) == 2
