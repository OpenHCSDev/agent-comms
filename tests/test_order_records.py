"""The order's commander judges tool calls and strikes units through the CLI tool path."""

import json
import os
import subprocess
import sys
import time

import pytest

from agent_comms.comms import wire
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.plain_text import PlainText, PlainTextError
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread
from agent_comms.todos import AcceptanceCheck, SourceTodoWhere, TodoStore, UnassignedTodoAssignment
from agent_comms.transcript_events import ToolStartTranscript


def invoke(comms, caller, tool, **arguments):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AGENT_COMMS_', 'PI_SESSION_', 'OPENCODE_SERVER_'))}
    result = subprocess.run(
        [sys.executable, '-m', 'agent_comms.cli', '--root', str(comms.root), 'invoke',
         '--from', caller, '--tool', tool, '--arguments', json.dumps(arguments)],
        env=environment, text=True, capture_output=True, timeout=30)
    return result.returncode, json.loads(result.stdout) if result.returncode == 0 else result.stdout + result.stderr


def test_plain_text_refuses_short_and_hidden_wording_but_not_names_it_cites():
    with pytest.raises(PlainTextError, match="full sentence"):
        PlainText.from_text("It broke the build.")
    with pytest.raises(PlainTextError, match="lifecycle, receipts"):
        PlainText.from_text("The unit changed the lifecycle and wrote receipts instead of fixing the bug.")
    cited = ('It edited toad-close-lifecycle and goal_history.py:32 and quoted "checkpoint" '
             'and `custody` without fixing the bug it was given.')
    assert PlainText.from_text(cited) == cited


def test_commander_accepts_sends_back_and_strikes_until_the_order_is_taken_away(tmp_path):
    comms = wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    (tmp_path / 'problem.txt').write_text('P51_PENDING\n')
    comms.registry.declare(Thread('lead', frozenset(), str(tmp_path), created_at=1.0))
    comms.registry.declare(Thread('other', frozenset(), str(tmp_path), created_at=2.0))
    comms.registry.declare(Thread('unit', frozenset(), str(tmp_path), created_at=3.0,
                                  execution=ExternalThreadExecution))
    store = TodoStore(comms.root)
    store.create('P1', 'Org/Repo', 'Remove the pending marker from the problem file',
                 creator=comms.registry.require('lead'),
                 where=SourceTodoWhere(str(tmp_path / 'problem.txt'), 1),
                 acceptance=AcceptanceCheck(str(tmp_path), 'grep -c P51_PENDING problem.txt || true', 0),
                 bus=comms.bus)
    store.assign('P1', expected_revision=1, owner=comms.registry.require('unit'),
                 parent=comms.registry.require('lead'), generation='order-1',
                 deadline=RecordedEventTime(time.time() + 3600), goals=comms.goals)
    order_text = comms.registry.require('unit').goal.text
    comms.transcripts.external('unit').finish(turn_id='t1', item_id='i1', events=(
        ToolStartTranscript(tool_call_id='edit-1', tool_name='edit',
                            raw_input={'path': 'problem.txt', 'edits': [{'newText': 'marker gone'}]}),
        ToolStartTranscript(tool_call_id='edit-2', tool_name='edit',
                            raw_input={'path': 'problem.txt', 'edits': [{'newText': 'P51_DONE'}]}),))
    call = dict(unit='unit', goal_id='order-1', tool_call='edit-1', quoted='marker gone',
                reason='The edit deletes the pending marker line the problem names, nothing more.')
    serves = ' '.join(order_text.split()[:4])

    refused, error = invoke(comms, 'other', 'comms_accept', serves=serves, **call)
    assert refused and "original commander" in error
    refused, error = invoke(comms, 'lead', 'comms_accept', serves=serves, **{**call, 'quoted': 'absent'})
    assert refused and 'does not contain' in error
    refused, error = invoke(comms, 'lead', 'comms_accept', serves='words no order said', **call)
    assert refused and 'exact words of the order' in error
    status, accepted = invoke(comms, 'lead', 'comms_accept', serves=serves, **call)
    assert status == 0 and accepted['verdict']['outcome'] == 'accepted'

    refused, error = invoke(comms, 'lead', 'comms_accept', serves=serves, **call)
    assert refused and 'already has a verdict' in error
    status, back = invoke(comms, 'lead', 'comms_send_back', text='Delete the whole line, not its text.',
                          **{**call, 'tool_call': 'edit-2', 'quoted': 'P51_DONE',
                             'reason': 'It renamed the marker to P51_DONE and left the line in place.'})
    assert status == 0, back
    assert (back['strikes'], back['fired']) == (1, False)
    goal = comms.registry.require('unit').goal
    assert goal.text.startswith('Delete the whole line') and goal.revision == 2
    refused, error = invoke(comms, 'lead', 'comms_send_back', text='Must not change the order again.',
                            **{**call, 'tool_call': 'edit-2', 'quoted': 'P51_DONE'})
    assert refused and 'already has a verdict' in error
    assert comms.registry.require('unit').goal == goal

    refused, error = invoke(comms, 'lead', 'comms_strike', unit='unit', goal_id='order-1', struck='unit',
                            cause='false_done', caught='review',
                            what_happened='It claimed the semantic fix was done while the marker stayed.')
    assert refused and 'semantic' in error
    status, struck = invoke(comms, 'lead', 'comms_strike', unit='unit', goal_id='order-1', struck='unit',
                            cause='false_done', caught='live',
                            what_happened='It reported done on toad-close-lifecycle while the marker was still there.')
    assert status == 0 and (struck['strikes'], struck['fired']) == (2, True)
    assert TodoStore(comms.root).get('P1').assignment == UnassignedTodoAssignment()
    assert comms.registry.require('unit').goal is None

    order = dict(unit='unit', goal_id='order-1')
    status, work = invoke(comms, 'other', 'comms_order_work', **order)
    assert status == 0 and [(c['call']['tool_call_id'], c['verdict']['outcome'])
                            for c in work['calls']] == [('edit-2', 'sent_back'), ('edit-1', 'accepted')]
    status, story = invoke(comms, 'other', 'comms_order_story', **order)
    assert status == 0 and len(story['verdicts']) == 2 and len(story['strikes']) == 1
    assert struck['row']['id'] == story['strikes'][0]['id']
    assert not (tmp_path / 'order_records.sqlite3').exists()
    assert story['history'][-1]['after'] is None  # the second strike cleared the order
    status, fleet = invoke(comms, 'other', 'comms_fleet_story', limit=5)
    assert status == 0 and [line['line'].split(':')[0] for line in fleet['strikes']] == ['unit', 'unit']
    assert 'false_done, caught at live by lead' in fleet['strikes'][0]['line']
