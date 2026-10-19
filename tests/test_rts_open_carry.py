"""The one-time move reads only ordered problems on a separate Core copy."""
import json
import shutil
import sqlite3

from cutover.issue_rts_orders import carry
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.messages import Message, InfoKind
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
    message = Message('claude-code', 'worker', f'''Problem: P1
Owner: {__file__}:1
1. Repair the existing bus reader
Banned: No second store
Report: Report the result
''', InfoKind, seq=1)
    (copied / 'order-messages.jsonl').write_text(json.dumps(message.to_wire()) + '\n')
    rts = tmp_path / 'rts'
    rts.mkdir()
    (rts / 'deadlines.json').write_text(json.dumps({'worker': [{
        'due': '00:30', 'msg': message.message_id, 'at': 100, 'fact': 'P1', 'facts': ['P1'],
        'check': [str(tmp_path), 'printf 1', 1], 'disk': False}]}))
    (rts / 'problems.json').write_text(json.dumps({
        'P1': {'text': row.text, 'where': 'reader.py:1', 'at': 90},
        'UNORDERED': {'not_a_problem_record': True}}))
    (rts / 'strikes.jsonl').write_text('\n'.join(json.dumps(record) for record in (
        {'unit': 'worker', 'why': 'Archived old strike', 'at': 50, 'task': None},
        {'unit': 'worker', 'why': 'The unit changed the wrong file and ignored the required edit',
         'at': 120, 'task': 'P1'})) + '\n')
    assert carry(rts, copied, True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['orders_issued'] == {'create': 0, 'assign': 1}
    assert report['unordered_problems'] == report['strikes_left_as_history'] == 1
    assert report['strikes_attached'] == 1
    assert report['blockers'] == {}
    assert TodoStore(comms.root).get('P1') == row
    issued = TodoStore(copied).get('P1')
    assert issued.order.id == message.message_id
    assert issued.strikes[0].previous == issued.held()
