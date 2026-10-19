"""The one-time move reads only ordered problems on a separate Core copy."""
import json
import shutil
import sqlite3
from dataclasses import replace

from cutover.carry_rts import carry
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import Assignment, NoGoalReference, OpenTodoState, Todo, TodoStore
from test_commander_problem_orders import order


def test_open_only_move_on_copied_root(commander, tmp_path, capsys):
    comms, _ = commander
    actor = comms.registry.new_thread('claude-code', frozenset(), str(tmp_path), created_at=3.0)
    comms.registry.declare(actor)
    worker = comms.registry.require('worker')
    assignment = Assignment(worker.name, worker.created_at, actor.name, actor.created_at,
                            worker.goal.require().id)
    row = Todo(id='P1', repo='OpenHCSDev/agent-comms', text='Repair the bus reader',
               creator=actor.name, creator_created=actor.created_at, state=OpenTodoState,
               revision=1, goal=NoGoalReference(), assignment=assignment,
               order=order(tmp_path, 'old-message'), deadline=RecordedEventTime(1000))
    with TodoStore(comms.root).writing() as db:
        row.upsert(db)
    copied = tmp_path / 'copy'
    copied.mkdir()
    shutil.copy2(comms.root / 'registry.json', copied / 'registry.json')
    with sqlite3.connect(comms.root / 'todos.sqlite3') as source, sqlite3.connect(copied / 'todos.sqlite3') as target:
        source.backup(target)
    rts = tmp_path / 'rts'
    rts.mkdir()
    (rts / 'deadlines.json').write_text(json.dumps({'worker': [{
        'due': '00:30', 'msg': row.order.id, 'at': 100, 'fact': 'P1', 'facts': ['P1'],
        'check': [str(tmp_path), 'printf 1', 1], 'disk': False}]}))
    (rts / 'problems.json').write_text(json.dumps({
        'P1': {'text': row.text, 'where': 'reader.py:1', 'at': 90},
        'UNORDERED': {'not_a_problem_record': True}}))
    (rts / 'strikes.jsonl').write_text(json.dumps({
        'unit': 'worker', 'why': 'Archived old strike', 'at': 50, 'task': None}) + '\n')
    assert carry(rts, copied, True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['carried'] == {'orders': 1, 'problems': 1, 'strikes': 0}
    assert report['archived_problems'] == report['archived_strikes'] == 1
    assert report['blockers'] == {}
    assert report['todo_writes'] == 1
    assert TodoStore(comms.root).get('P1') == row
    assert carry(rts, copied, True) == 0
    assert json.loads(capsys.readouterr().out)['todo_writes'] == 0
