"""Strike citations and completion use their existing value and delivery owners."""
import json
import subprocess
import time

import pytest

from agent_comms.change_report import FileLine
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import DeliverTodo, StrikeTodo
from agent_comms.tools import CommsChangeProblemTool, CommsOrderDoneTool
from test_commander_problem_orders import create


def test_strike_refuses_missing_or_nonexistent_evidence_before_recording(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    actor = comms.registry.require('lead').incarnation
    reason = 'The unit delivered the wrong source change without repairing the requested behavior.'
    for evidence, error in (((), 'checkable file:line'),
                            ((FileLine('/no-such-p710-source.py', 1),), 'does not exist')):
        with pytest.raises(ValueError, match=error):
            CommsChangeProblemTool(problem='P1', operation=StrikeTodo(
                todo.revision, actor, todo.assignment, reason, evidence=evidence)).apply(comms, caller)
        assert comms.goals.todos.get('P1') == todo
    evidence = (FileLine(__file__, 1),)
    CommsChangeProblemTool(problem='P1', operation=StrikeTodo(
        todo.revision, actor, todo.assignment, reason, evidence=evidence)).apply(comms, caller)
    recorded = comms.goals.todos.get('P1').strikes
    assert len(recorded) == 1 and recorded[0].evidence == evidence
    assert FieldCodec.decode(StrikeTodo, FieldCodec.encode(recorded[0])) == recorded[0]


def test_real_delivery_tool_decodes_once_and_queries_origin_once(commander, monkeypatch):
    comms, caller = commander
    source = comms.root

    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()

    git('init', '-q')
    (source / 'answer.py').write_text('answer = 1\n')
    git('add', 'answer.py')
    git('-c', 'user.name=check', '-c', 'user.email=check@example.org', 'commit', '-qm', 'answer')
    commit = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', commit)
    todo = create(commander, 'P1', deadline=time.time() + 1000)
    trace = source / 'git-trace.jsonl'
    monkeypatch.setenv('GIT_TRACE2_EVENT', str(trace))
    CommsOrderDoneTool.invoke(comms, dict(problem='P1', order_id=todo.order.id,
        delivery=dict(kind='delivered_commit', commit=commit, delivered_commit=commit),
        claims=[dict(kind='merged', commit=commit)]), caller=caller)
    commands = [event['argv'] for line in trace.read_text().splitlines()
                if (event := json.loads(line)).get('event') == 'start'
                and any('origin' in arg for arg in event.get('argv', []))]
    assert len(commands) == 1 and 'for-each-ref' in commands[0]
    assert not comms.goals.todos.get('P1').pending
    assert 'claims' in CommsOrderDoneTool.schema()['parameters']['properties']
    assert 'verified_claims' not in FieldCodec.record_schema(DeliverTodo)['properties']
