"""Order policy reads actual TodoStore rows through the declared commander verbs."""
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.plan_phases import PlanPhase, RecordPlanePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
from agent_comms.todos import AssignTodo, WithdrawTodo
from test_commander_problem_orders import create, order_body


def fresh_rules(comms):
    reviews = RuleReviews(comms.root)
    reviews.write('lead', DrawnRuleReviewEntry(('Keep orders in their Core owner.',), 0))
    reviews.write('lead', AnsweredRuleReviewEntry(
        1, 'P244 keeps its deadline in the Todo and every gate reads that owner.'))


def test_stale_gate_covers_its_verb_family_but_allows_board_repairs(
        commander, commander_script, tmp_path):
    comms, _ = commander
    script, _ = commander_script
    fresh_rules(comms)
    todo = create(commander, 'P244', deadline=time.time() - 1900)
    body = str(tmp_path / 'must-not-be-read')
    blocked = (
        ['send', 'worker', '23:59', body], ['say', 'worker', body],
        ['problem', 'repair the owner', '--to', 'worker', '--due', '23:59',
         '--order', body, '--where', f'{__file__}:1'],
        ['strike', 'worker', '--why', 'P244 missed its deadline'],
    )
    for args in blocked:
        with pytest.raises(SystemExit) as refused:
            script.OrderVerb.parse(args, '').apply()
        text = str(refused.value)
        assert 'P244: holder worker, deadline ' in text
        assert 'more than 30 minutes overdue' in text
    why = 'The current source needs one more bounded check before delivery.'
    repairs = (
        ['due', 'P244', '23:59', '--why', why], ['withdraw', 'P244', '--why', why],
        ['done', 'worker', 'order-P244', '--sha', 'a' * 40],
        ['close', 'P244', '--works', why],
        ['check', 'P244', '--check', f'{tmp_path} :: printf 0 -> 0', '--why', why],
        ['serve', 'P244', 'record_plane'], ['drop', 'worker', 'old', '--by', 'new'],
    )
    for args in repairs:
        script.OrderVerb.parse(args, '').gate()
    actor = comms.registry.require('lead').incarnation
    comms.goals.todos.apply(todo.id, AssignTodo(
        todo.revision, todo.held(), RecordedEventTime(time.time() + 3600), todo.order, todo.goal))
    script.OrderVerb.parse(blocked[0], '').gate()
    moved = comms.goals.todos.get(todo.id)
    comms.goals.todos.apply(todo.id, WithdrawTodo(
        moved.revision, actor, moved.held(), why, None))
    script.OrderVerb.parse(blocked[0], '').gate()


@pytest.mark.parametrize('phase', (UnplannedPhase, *PlanPhase.members_with(PlanPhase)))
def test_phase_gate_uses_the_phase_family_and_requires_an_explicit_unplanned_reason(
        commander, commander_script, phase):
    comms, _ = commander
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    body = Path(order_body(repo)).read_text()
    if phase is UnplannedPhase:
        with pytest.raises(SystemExit, match='Plan phases are ready'):
            script.send('worker', '23:59', body, clean=True, new=('P1', 'Repair the owner'))
        assert comms.goals.todos.for_commander(comms.registry.require('lead').incarnation) == ()
        reason = 'This urgent retained incident is outside the declared plan dependency chain.'
    else:
        reason = ''
    body += f'\nServes: {phase.declared_name} {reason}\n'
    script.send('worker', '23:59', body, clean=True, new=('P1', 'Repair the owner'))
    assert comms.goals.todos.get('P1').order.serves is phase


def test_second_plan_order_gate_names_the_busy_order_and_running_unowned_units(
        commander, commander_script):
    comms, _ = commander
    script, repo = commander_script
    todo = create(commander, 'P1')
    with comms.goals.todos.writing() as db:
        replace(todo, order=replace(todo.order, serves=RecordPlanePhase)).upsert(db)
    comms.registry.declare(comms.registry.new_thread('free-unit', frozenset(), str(repo)))
    comms.owners.acquire_thread('free-unit', owner_pid=os.getpid())
    (repo / 'count.txt').write_text('0\n')
    body = Path(order_body(repo)).read_text() + '\nServes: record_plane\n'
    with pytest.raises(SystemExit) as refused:
        script.send('worker', '23:59', body, clean=True, new=('P2', 'Repair another owner'))
    assert 'worker already holds plan orders P1' in str(refused.value)
    assert 'running units holding no order: free-unit' in str(refused.value)
    assert [todo.id for todo in comms.goals.todos.for_commander(
        comms.registry.require('lead').incarnation)] == ['P1']
