"""One unit Goal reads its independent canonical Todo orders through real tools."""
import asyncio
from pathlib import Path

import pytest

from order_scope_fixture import FIX_SCOPE


from agent_comms.change_report import FileLine
from agent_comms.child_process import BoundedRun
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.tools import (CommsTodoCreateTool)
from agent_comms.todos import (
    GoalRef,
    OrderDelivery,
    RecordedCommitDelivery,
    PendingProblemOrder,
    Slop,
    DeclarationDerivation,
)
from agent_comms.order_runs import MechanicalOrderRun


def order(root, identity, command='printf 1', expected=1):
    return PendingProblemOrder(id=identity, owner=FileLine(__file__, 1), scope=FIX_SCOPE, run=MechanicalOrderRun(),
        check=OrderCheck(str(root), command, CountCheckResult(expected)),
        edits=('Repair the existing owner',), banned=('No second store',), report='Report the result',
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))


def create(commander, problem, command='printf 1', expected=1, review_at=1000, owner='worker'):
    comms, caller = commander
    CommsTodoCreateTool(owner=owner, order=CreateProblemGoalAction(
        problem=problem, order=order(comms.root, 'order-' + problem, command, expected),
        request='request', review_at=RecordedEventTime(review_at), commander='lead',
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
    with pytest.raises(ValueError):
        UnrecordedEventTime().require_delivery()


def test_slop_uses_every_pinned_catalog_entry_and_refuses_unknown_ids():
    location = FileLine(__file__, 1)
    for pattern in Slop.patterns():
        slop = Slop(pattern, 'the count of open orders', location, DeclarationDerivation, location)
        assert FieldCodec.decode(Slop, FieldCodec.encode(slop)) == slop
        assert pattern in slop.render()
    with pytest.raises(ValueError):
        Slop('MADEUP-999', 'the count of open orders', location, DeclarationDerivation, location)


def problem(script, body):
    script.problem(body, 'worker', f'{__file__}:1')


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
        with pytest.raises(ValueError):
            CountCheckResult(1).require(ran)


FIRED = 'The unit took two strikes on this problem and its second delivery was late again.'


def stray(commander, problem, generation):
    """A pending problem stored the way pre-P48 joins and refused sends left them: under a Goal id that is
    not the unit's open Goal."""
    comms, _ = commander
    lead = comms.registry.require('lead')
    return comms.goals.todos.create(
        problem, RepositoryName.from_wire('OpenHCSDev/agent-comms'), 'Fix ' + problem, creator=lead,
        owner=comms.registry.require('worker'), generation=generation, review_at=RecordedEventTime(1000),
        order=order(comms.root, 'order-' + problem), goal=GoalRef('lead', lead.created_at, 'request'))




