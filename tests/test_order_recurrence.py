"""Recurring problems must replace their duplicated fact with a declaration."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.change_report import FileLine
from agent_comms.commander.order import OrderVerb
from agent_comms.todos import Derivation, LocatedProblemFinding, Slop, TodoError
from test_commander_problem_orders import create, order_body


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


@pytest.mark.parametrize('derive', ('declaration', 'subclass', 'mixin', 'base-method',
                                    'init-subclass', 'metaclass'))
def test_declaration_derivations_require_another_file(derive, tmp_path):
    mechanism = Derivation.member_named(derive)
    where = FileLine('broken.py', 1)
    assert mechanism.declaration_elsewhere(FileLine('owner.py', 1), where, tmp_path)
    assert not mechanism.declaration_elsewhere(FileLine(str(tmp_path / 'broken.py'), 9), where, tmp_path)


@pytest.mark.parametrize(('age', 'refused'), ((7 * 86400, True), (7 * 86400 + 1, False), (-1, False)))
def test_recurrence_uses_the_recorded_seven_day_window(commander, age, refused):
    comms, _ = commander
    todo = create(commander, 'P1')
    previous = todo.order.checked()
    assert bool(todo.recurrence((previous.owner,), comms.root, 100 + age)) is refused


def test_recurrence_also_matches_a_previous_where(commander):
    comms, _ = commander
    todo = create(commander, 'P1')
    previous = replace(todo.order.checked(),
                       finding=LocatedProblemFinding(FileLine('broken.py', 1)))
    todo = replace(todo, order=previous)
    refusal, = todo.recurrence((FileLine('broken.py', 9),), comms.root, 101)
    assert previous.owner.to_wire() in refusal and 'Where: broken.py:1' in refusal
