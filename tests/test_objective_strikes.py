"""One real TodoStore journey drives every objective failure and its public line."""
import subprocess
import time
from contextlib import nullcontext
from dataclasses import replace

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.child_process import OrderCheckFailed
from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import AssignTodo, CommitDelivery, ObjectiveStrike
from agent_comms.tools import CommsOrderDoneTool
from test_commander_problem_orders import create, working


def test_objective_family_records_once_posts_minutes_and_uses_existing_takeaway(
        commander, tmp_path):
    comms, caller = commander
    store = comms.goals.todos
    now = time.time()
    todo = create(commander, 'P1', deadline=now + 120)
    with store.writing() as db:
        replace(todo, order=replace(todo.order, at=RecordedEventTime(now - 180),
            first_call_deadline=RecordedEventTime(now - 60))).upsert(db)

    def watch():
        UnownedWheelSpin.watch_pass(lambda: nullcontext(comms))

    def lines():
        return [message.body for message in comms.views.history_page(
            CommandTarget.named('#comms'), viewer='lead').messages
            if message.body.startswith('Core strike')]

    watch()
    first = store.get('P1')
    assert len(first.strikes) == 1 and first.holder == 'worker'
    assert 'made no first tool call' in lines()[0] and 'minutes late' in lines()[0]
    watch()
    assert store.get('P1') == first and len(lines()) == 1
    store.apply('P1', AssignTodo(first.revision, first.held(), RecordedEventTime(now - 120),
                              first.order, first.goal))
    watch()
    taken = store.get('P1')
    assert taken.holder is None and len(taken.strikes) == 2 and len(taken.released) == 1
    assert any('P1 taken away' in line and 'missed its order deadline' in line for line in lines())
    watch()
    assert store.get('P1') == taken and len(lines()) == 2

    repo, origin = tmp_path / 'source', tmp_path / 'origin.git'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    for args in (('init', '-q', '-b', 'main'), ('remote', 'add', 'origin', str(origin))):
        subprocess.run(['git', '-C', str(repo), *args], check=True)
    (repo / 'count.txt').write_text('0\n')
    for args in (('add', '.'), ('-c', 'user.name=t', '-c', 'user.email=t@t',
                             'commit', '-q', '-m', 'delivered failing source'),
                 ('push', '-q', 'origin', 'main')):
        subprocess.run(['git', '-C', str(repo), *args], check=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    todo = create(commander, 'P2', deadline=now - 60)
    with store.writing() as db:
        replace(todo, order=replace(todo.order, at=RecordedEventTime(now - 120),
            first_call_deadline=RecordedEventTime(now + 60),
            check=OrderCheck(str(repo), 'cat count.txt', CountCheckResult(1)))).upsert(db)
    done = CommsOrderDoneTool(problem='P2', order_id=todo.order.id,
        delivery=CommitDelivery(commit=sha, delivered_commit=sha), claims=working(comms))
    for _ in range(2):
        with pytest.raises(OrderCheckFailed):
            done.apply(comms, caller)
        assert len(store.get('P2').strikes) == 1
    assert len(lines()) == 3
    assert any(sha in line and 'order check failed' in line and 'minutes' in line for line in lines())
    found = {type(strike) for pid in ('P1', 'P2') for strike in store.get(pid).strikes}
    assert found == set(ObjectiveStrike.members_with(ObjectiveStrike))
    assert store.get('P2').pending  # A failed check never becomes delivered work.
    failed = store.get('P2')
    with store.writing() as db:
        replace(failed, order=replace(failed.order,
            check=OrderCheck(str(repo), 'cat count.txt', CountCheckResult(0)))).upsert(db)
    done.apply(comms, caller)
    delivered = store.get('P2')
    assert not delivered.pending and delivered.holder is None
    assert len(delivered.strikes) == 2 and len(delivered.released) == 1 and len(lines()) == 4

    # A subsequent late done must not create a second copy of the watcher's deadline failure.
    third = create(commander, 'P3', deadline=now - 60)
    with store.writing() as db:
        replace(third, order=replace(third.order, at=RecordedEventTime(now - 180),
            first_call_deadline=RecordedEventTime(now + 60),
            check=OrderCheck(str(repo), 'cat count.txt', CountCheckResult(0)))).upsert(db)
    store.advance_objective_failures(comms.goals)
    assert len(store.get('P3').strikes) == 1
    CommsOrderDoneTool(problem='P3', order_id=third.order.id,
        delivery=CommitDelivery(commit=sha, delivered_commit=sha), claims=working(comms)).apply(comms, caller)
    assert not store.get('P3').pending and len(store.get('P3').strikes) == 1
    assert len(lines()) == 5
