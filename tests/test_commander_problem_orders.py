"""One unit Goal reads its independent canonical Todo orders through real tools."""
import asyncio
import importlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from contextlib import closing
from dataclasses import replace

from cutover.carry import CarryChain

from agent_comms.change_report import FileLine
from agent_comms.commander import order as commander_order
from agent_comms.commander.config import Repo
from agent_comms.channel_targets import CommandTarget
from agent_comms.child_process import BoundedRun
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_actions import BlockedGoalAction, CompletedGoalAction, CreateProblemGoalAction, ModelInvocable, OrderGoalAction, SetGoalAction
from agent_comms.goal_states import CompletedGoal
from agent_comms.goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (CommsCorrectWordingTool, CommsSendTool, CommsFireTool, CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderTool,
    CommsOrderWorkTool, CommsProblemCloseTool, CommsTodoCreateTool, CommsTodoListTool, CommsChangeProblemTool, CommsVerdictTool)
from agent_comms.todos import (VerdictAudience, OrderMessage, OrderDeletion, ModuleDeletion, NoOrderDeletion, GoalRef, CleanHunt, CommitDelivery, Hunt, OrderDelivery,
    ApplicationOrderRun, RecordedCommitDelivery, MechanicalOrderRun, TodoStore,
    OperationDelivery, OrderedHunt,
    PendingProblemOrder,
    AcceptedVerdict, Assignment, AssignTodo, DoneTodoState, ReleaseTodo, SendBackVerdict, StrikeReason,
    StrikeTodo, Todo, TodoConflict, TodoError, VoidStrikeTodo, Slop, DeclarationDerivation)
from agent_comms.turn_context import GoalSegment


def order(root, identity, command='printf 1', expected=1):
    return PendingProblemOrder(id=identity, owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(root), command, CountCheckResult(expected)),
        edits=('Repair the existing owner',), banned=('No second store',), report='Report the result',
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))


def create(commander, problem, command='printf 1', expected=1, deadline=1000, owner='worker'):
    comms, caller = commander
    CommsTodoCreateTool(owner=owner, order=CreateProblemGoalAction(
        problem=problem, order=order(comms.root, 'order-' + problem, command, expected),
        request='request', deadline=RecordedEventTime(deadline), commander='lead',
        repo=RepositoryName.from_wire('OpenHCSDev/agent-comms'), text='Fix ' + problem,
    )).apply(comms, caller)
    return comms.goals.todos.get(problem)


def working(comms):
    """The working claim a delivery stands on: a screenshot that exists."""
    screenshot = Path(comms.root) / 'working.png'
    screenshot.write_bytes(b'png')
    return (WorkingClaim(str(screenshot)),)


def test_a_done_record_from_before_the_split_reads_its_one_commit_as_both():
    old = {'kind': 'commit', 'commit': 'd012d23b4', 'at': FieldCodec.encode(RecordedEventTime(1000))}
    recorded = FieldCodec.decode(OrderDelivery, old)
    assert type(recorded) is RecordedCommitDelivery and recorded.delivered == recorded.commit == 'd012d23b4'
    assert FieldCodec.encode(recorded) == old


def test_delivery_time_has_its_own_named_requirement():
    with pytest.raises(ValueError, match='delivery'):
        UnrecordedEventTime().require_delivery()


def test_slop_uses_every_pinned_catalog_entry_and_refuses_unknown_ids():
    location = FileLine(__file__, 1)
    for pattern in Slop.patterns():
        slop = Slop(pattern, 'the count of open orders', location, DeclarationDerivation, location)
        assert FieldCodec.decode(Slop, FieldCodec.encode(slop)) == slop
        assert pattern in slop.render()
    with pytest.raises(ValueError, match='MADEUP-999'):
        Slop('MADEUP-999', 'the count of open orders', location, DeclarationDerivation, location)


def order_body(repo, check=None, edit='Set count.txt to 1.', extra='', deletes=NoOrderDeletion()):
    """Raw JSON input; the application, not the test, constructs the order."""
    body = repo.parent / 'order.json'
    checked = OrderCheck.parse(f'{repo} :: {check or "cat count.txt -> 1"}')
    body.write_text(json.dumps({
        'defect': 'The counter file holds zero where the fleet needs one',
        'why_stupid': 'The counter is wrong and that is stupid.',
        'unplanned_reason': 'This isolated order checks command behavior rather than advancing the plan dependency chain.',
        'order': {'kind': 'pending', 'owner': f'{__file__}:1',
                  'check': FieldCodec.encode(checked), 'edits': [edit],
                  'deletes': FieldCodec.encode(deletes),
                  'run': {'kind': 'application', 'text': 'the installed app shows the counter at one after restart'},
                  'banned': ['no second store', 'no cache'],
                  'report': 'the commit and the count before -> after ' + extra}}))
    return str(body)


def problem(script, body):
    script.problem(body, 'worker', RecordedEventTime.from_clock('23:59'), f'{__file__}:1')


@pytest.mark.parametrize('printed, refused', [
    ("printf 'no count here'", 'The order check cannot prove CountCheckResult(expected=1): it printed no integer.'),
    ('echo 2', 'The order check is 1 away from CountCheckResult(expected=1).'),
    ('echo found 1', None),
])
def test_a_count_check_refuses_saying_whether_it_printed_no_integer_or_the_wrong_count(tmp_path, printed, refused):
    ran = asyncio.run(BoundedRun.run(('bash', '-c', printed), cwd=str(tmp_path), timeout=30))
    if refused is None:
        CountCheckResult(1).require(ran)
    else:
        with pytest.raises(ValueError, match=re.escape(refused)):
            CountCheckResult(1).require(ran)


FIRED = 'The unit took two strikes on this problem and its second delivery was late again.'


def stray(commander, problem, generation):
    """A pending problem stored the way pre-P48 joins and refused sends left them: under a Goal id that is
    not the unit's open Goal."""
    comms, _ = commander
    lead = comms.registry.require('lead')
    return comms.goals.todos.create(
        problem, RepositoryName.from_wire('OpenHCSDev/agent-comms'), 'Fix ' + problem, creator=lead,
        owner=comms.registry.require('worker'), generation=generation, deadline=RecordedEventTime(1000),
        order=order(comms.root, 'order-' + problem), goal=GoalRef('lead', lead.created_at, 'request'))


def native_judge_edits(commander, judge, edits):
    """Use the actual native reader and original call IDs, never an edit journal."""
    comms, _ = commander
    path = comms.root / 'judge-session.jsonl'
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(199., timezone.utc).isoformat()
    rows = [dict(type='session', id='judge-session', version=3, timestamp=stamp),
            dict(type='message', id='input', timestamp=stamp, message=dict(role='user', content='Repair the owner', timestamp=199000))]
    parent = 'input'
    for edit in edits:
        entry = 'entry-' + edit['id']
        rows.append(dict(type='message', id=entry, parentId=parent,
            timestamp=datetime.fromtimestamp(max(200., edit['at']), timezone.utc).isoformat(), message=dict(
            role='assistant', timestamp=max(200., edit['at']) * 1000, stopReason='toolUse', content=[dict(
                type='toolCall', id=edit['id'], name='edit', arguments=dict(path=edit['path'],
                edits=[dict(oldText=h['old'], newText=h['new']) for h in edit['hunks']]))])))
        parent = entry
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    path.chmod(0o600)
    comms.registry.register(replace(comms.registry.require('worker'), session_file=str(path)))
    return {edit.id: edit for edit in judge.Edit.unjudged(comms, 'lead')}


VERDICT_LINES = {
    'ok': "e1:ab --msg m1 --line one --why why --serves 'Repair the existing owner'",
    'superseded': "e1:ab --line one --why 'The task moved to a different implementation owner' --by m2",
    'back': "e1:ab --msg m1 --line one --why 'The unit wrapped the old store' --roast r --fix f --slop IDEN-5"
            " --fact 'four words of fact' --at a.py:1 --derive delete-copy --by b.py:2",
}


