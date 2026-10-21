"""Retained Git work protects linked rows through the real watcher and heartbeat."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent_comms.candidate_maintenance import drain_candidate_catchup
from agent_comms.change_report import FileLine
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.standing import (
    CheckoutHeldWheelSpin,
    ReassignedWheelSpin,
    UnownedOrderWatch,
    UnownedWheelSpin,
    WaitingUnownedWheelSpin,
)
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderReview
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import OrderCheck, RequestGoalOrder
from agent_comms.installer.practice import practice_copy
from agent_comms.launch_root import LaunchRoot
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.todos import GoalRef, PendingProblemOrder, ReleaseTodo, StrikeReason
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.work_branches import WorkBranchError


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ('git', '-C', str(directory), '-c', 'user.name=Checkout fixture',
         '-c', 'user.email=checkout@example.invalid', *arguments),
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout


@pytest.fixture
def checkout_bus(tmp_path: Path) -> Comms:
    home = tmp_path / 'declared-project'
    home.mkdir()
    git(home, 'init', '-q', '--initial-branch=main')
    (home / 'work.py').write_text('# published work\n')
    git(home, 'add', 'work.py')
    git(home, 'commit', '-q', '-m', 'Published fixture')
    remote = tmp_path / 'published.git'
    git(tmp_path, 'init', '-q', '--bare', str(remote))
    git(home, 'remote', 'add', 'origin', str(remote))
    git(home, 'push', '-q', '-u', 'origin', 'main')
    rule = StorageRule(checkout_base=tmp_path / 'checkouts',
                       scratch_base=tmp_path / 'scratch', volume=tmp_path)
    checkout = rule.order_checkout('worker', 'P1')
    checkout.parent.mkdir()
    git(home, 'worktree', 'add', '-q', '-b', 'worker-work', str(checkout))
    git(checkout, 'push', '-q', '-u', 'origin', 'worker-work')
    root = tmp_path / 'seed'
    root.mkdir(mode=0o700)
    (root / 'storage.json').write_text(json.dumps(FieldCodec.encode(rule)))
    c = Comms(root)
    c.messaging.initialize_private_initial_protocol()
    for name in ('lead', 'worker', 'idle'):
        c.registry.declare(c.registry.new_thread(
            name, frozenset(), str(home), execution=ExternalThreadExecution))
    c.goals.update_goal('lead', SetGoalAction(
        text='Keep changes with their author', order=RequestGoalOrder(), goal_id='request'))
    lead, worker = c.registry.require('lead'), c.registry.require('worker')
    now = time.time()
    order = PendingProblemOrder(
        id='shared-work', owner=FileLine('work.py', 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(home), 'true'),
        edits=('Keep the changes with their author until they have been pushed.',),
        banned=('No second store of checkout holders.',), report='Report the actual checkout.',
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120))
    for problem in ('P1', 'P2'):
        c.goals.todos.create(
            problem, RepositoryName.from_wire('OpenHCSDev/agent-comms'),
            'Keep changes with their author', creator=lead, owner=worker,
            generation='shared-work', review_at=RecordedEventTime(now + 3600),
            order=order, goal=GoalRef('lead', lead.created_at, 'request'))
    row = c.goals.todos.get('P2')
    c.goals.todos.apply('P2', ReleaseTodo(
        row.revision, CoreWatcherIncarnation.watcher(), row.held(),
        StrikeReason('The unit stopped answering this order before its checked work was finished.')))
    c.transcripts.external('idle').finish(
        turn_id='available', item_id='answer', events=(AssistantTranscript(
            'Ready for work', position='answer', timestamp=time.time()),))
    CommanderHeartbeat.register(c.registry)
    CommanderConfig(
        commander='lead', root=root, state=tmp_path / 'state', repos=(),
        worktrees=rule.checkout_base, scratch=rule.scratch_base, disk=tmp_path,
    ).write(tmp_path / 'config-home')
    return c


def assert_retained(c: Comms) -> None:
    before = c.goals.todos.get('P2')
    rules = UnownedWheelSpin.violations(c, 'lead')
    assert len(rules) == 1
    held = rules[0]
    assert isinstance(held, CheckoutHeldWheelSpin)
    assert held.work.owner.name == 'worker'
    assert held.work.checkout == c.registry.storage_rule.order_checkout('worker', 'P1')
    assert 'worker' in held.line() and str(held.work.checkout) in held.line()
    # The real automatic caller must not bypass the heartbeat's protection.
    UnownedOrderWatch().pass_body(c)
    assert c.goals.todos.get('P2') == before
    assert before.holder is None


def test_dirty_then_unpushed_then_pushed_linked_checkout(checkout_bus: Comms, tmp_path: Path):
    original = checkout_bus
    untouched = original.goals.todos.get('P2')
    idle_goal = original.registry.require('idle').goal
    checkout = original.registry.storage_rule.order_checkout('worker', 'P1')
    with practice_copy(original.root, tmp_path / 'practice') as (root, _measurement):
        c = Comms(root)
        review = CommanderReview(frozenset())
        with LaunchRoot.from_path(root).selected():
            (checkout / 'work.py').write_text('# not yet published\n')
            assert_retained(c)
            review.wake(c, CommanderHeartbeat.sender, c.registry.require('lead'))
            git(checkout, 'add', 'work.py')
            git(checkout, 'commit', '-q', '-m', 'Unpushed work')
            assert git(checkout, 'status', '--porcelain') == ''
            assert_retained(c)
            git(checkout, 'push', '-q', 'origin', 'worker-work')
            config = CommanderConfig.read_root(root)
            config.model_copy(update={'parked': frozenset({'idle'})}).write(tmp_path / 'copy-home')
            waiting = UnownedWheelSpin.violations(c, 'lead')
            assert len(waiting) == 1
            assert isinstance(waiting[0], WaitingUnownedWheelSpin)
            UnownedOrderWatch().pass_body(c)
            assert c.goals.todos.get('P2').holder is None
            review.wake(c, CommanderHeartbeat.sender, c.registry.require('lead'))
            config.write(tmp_path / 'copy-home')
            available = UnownedWheelSpin.violations(c, 'lead')
            assert len(available) == 1
            assert isinstance(available[0], ReassignedWheelSpin)
            assert available[0].unit == 'idle'
            assert set(available).isdisjoint(review.violations)
            UnownedOrderWatch().pass_body(c)
            assigned = c.goals.todos.get('P2')
            assert assigned.holder == 'idle'
            assert c.registry.require('idle').goal.require().text
            review.wake(c, CommanderHeartbeat.sender, c.registry.require('lead'))
        drain_candidate_catchup(root)
        history = subprocess.run(
            (sys.executable, '-m', 'agent_comms.cli', '--root', str(root), 'history',
             '--target', '@commander-heartbeat', '--as', 'lead', '--limit', '10'),
            env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=True)
        assert f'WORK HELD: P2 worker retains work in {checkout}' in history.stdout
        assert f'NO LONGER CURRENT: WORK HELD: P2 worker retains work in {checkout}' in history.stdout
        print('PRIVATE HEARTBEAT', history.stdout)
        print('PRIVATE WATCHER: dirty and unpushed retained; clean pushed assigned to', assigned.holder)
    assert original.goals.todos.get('P2') == untouched
    assert original.registry.require('idle').goal == idle_goal


def test_unreadable_checkout_is_not_clean(checkout_bus: Comms, tmp_path: Path):
    with practice_copy(checkout_bus.root, tmp_path / 'practice') as (root, _measurement):
        c = Comms(root)
        checkout = c.registry.storage_rule.order_checkout('worker', 'P1')
        (checkout / '.git').unlink()
        before = c.goals.todos.get('P2')
        with pytest.raises(WorkBranchError):
            UnownedOrderWatch().pass_body(c)
        assert c.goals.todos.get('P2') == before
