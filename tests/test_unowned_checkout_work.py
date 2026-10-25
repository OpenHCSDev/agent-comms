"""Checkout allocation and unpublished-file protection at their storage owner."""


from agent_comms.bus import Bus

import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from order_scope_fixture import FIX_SCOPE
from agent_comms.change_report import FileLine
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import (
    CommanderHeartbeat,
    CommanderObservation,
)
from agent_comms.comms import Comms, wire
from agent_comms.field_codec import FieldCodec, GoalId
from agent_comms.goals import OrderCheck
from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile
from agent_comms.launch_root import LaunchRoot
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.todos import (
    PendingProblemOrder, ProgressRead, ReleaseTodo, StrikeReason, TodoError,
)
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.turn_item import NativeTurnId
from agent_comms.ui_model.unit_state import UnitState, Unreadable
from agent_comms.work_branches import UnreadableCheckoutRead, WorkBranches
from agent_comms.threads import Thread


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ('git', '-C', str(directory), '-c', 'user.name=Checkout fixture',
         '-c', 'user.email=checkout@example.invalid', *arguments),
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout


def test_one_problem_owns_a_checkout_in_each_repository_it_changes(checkout_bus, tmp_path):
    rule = checkout_bus.registry.storage_rule
    worker = checkout_bus.registry.require('worker')
    checkout = rule.order_checkout(rule.initial_roots('worker'), 'P1')
    other = tmp_path / 'toad'
    other.mkdir()
    git(other, 'init', '-q', '--initial-branch=main')
    git(other, 'commit', '-q', '--allow-empty', '-m', 'Second repository')
    git(other, 'remote', 'add', 'origin', 'https://github.com/OpenHCSDev/toad.git')
    lockstep = rule.repository_checkout(
        rule.initial_roots('worker'), 'P1', RepositoryName('OpenHCSDev', 'toad'))
    git(other, 'worktree', 'add', '-q', '-b', 'worker-toad', str(lockstep))
    misnamed = checkout.with_name('worker--P1--textual')
    git(other, 'worktree', 'add', '-q', '-b', 'worker-misnamed', str(misnamed))

    assert checkout.name == 'worker--P1'
    assert lockstep.name == 'worker--P1--toad'
    assert rule.owns_present_checkout(worker, checkout)
    assert rule.owns_checkout(worker, lockstep, RepositoryName('OpenHCSDev', 'toad'))
    assert not rule.owns_checkout(worker, misnamed, RepositoryName('OpenHCSDev', 'toad'))
    assert rule.problem_checkouts(
        rule.initial_roots('worker'), 'P1') == (checkout, misnamed, lockstep)
    assert lockstep in rule.checkouts_of(worker)
    assert misnamed not in rule.checkouts_of(worker)


@pytest.fixture
def checkout_bus(tmp_path: Path, configured_repository, installed_runtime) -> Comms:
    available_turn_id = NativeTurnId('available')
    other_work_goal_id = GoalId.mint()
    reissued_work_goal_id = GoalId.mint()
    shared_work_goal_id = GoalId.mint()
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
                       scratch_base=tmp_path / 'scratch', volume=tmp_path, floor_gib=0)
    checkout = rule.order_checkout(rule.initial_roots('worker'), 'P1')
    checkout.parent.mkdir()
    git(home, 'worktree', 'add', '-q', '-b', 'worker-work', str(checkout))
    git(checkout, 'push', '-q', '-u', 'origin', 'worker-work')
    root = tmp_path / 'seed'
    root.mkdir(mode=0o700)
    (root / 'storage.json').write_text(json.dumps(FieldCodec.encode(rule)))
    Bus.install(Bus.path_in(root))
    c = wire(root)
    # Two order holders never share one worktree, so the unit taking the work has its own.
    spare = tmp_path / 'idle-project'
    git(home, 'worktree', 'add', '-q', '-b', 'idle-home', str(spare))
    for name in ('lead', 'worker'):
        c.registry.declare(Thread(
            name, frozenset(), str(home), execution=ExternalThreadExecution))
    c.registry.declare(Thread(
        'idle', frozenset(), str(spare), execution=ExternalThreadExecution,
        process_identity=ProcessIdentity.capture(os.getpid())))
    request = c.goals.todos.add_request(
        c.registry.require('lead').incarnation, 'Keep changes with their author').id
    lead, worker = c.registry.require('lead'), c.registry.require('worker')
    now = time.time()
    order = PendingProblemOrder(
        id='shared-work', owner=FileLine('work.py', 1), scope=FIX_SCOPE, run=MechanicalOrderRun(),
        check=OrderCheck(home, 'false'),
        edits=('Keep the changes with their author until they have been pushed.',),
        banned=('No second store of checkout holders.',), report='Report the actual checkout.',
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120))
    for problem in ('P1', 'P2'):
        c.goals.todos.create(
            problem, RepositoryName.from_wire('OpenHCSDev/agent-comms'),
            'Keep changes with their author', creator=lead, assigner=lead.incarnation, owner=worker,
            generation=shared_work_goal_id, review_at=RecordedEventTime(now + 3600),
            order=order, request=request)
    # Both current IDs change, as they do when Core hands work out again.
    for problem in ('P1', 'P2'):
        row = c.goals.todos.get(problem)
        c.goals.todos.assign(
            problem, expected_revision=row.revision, owner=worker,
            assigner=lead.incarnation, generation=reissued_work_goal_id,
            review_at=RecordedEventTime(now + 3600), order=order.reissued(now + 1),
            request=request)
    c.goals.todos.create(
        'P3', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Keep unrelated work separate', creator=lead, assigner=lead.incarnation, owner=worker,
        generation=other_work_goal_id, review_at=RecordedEventTime(now + 3600),
        order=order.reissued(now + 2), request=request)
    row = c.goals.todos.get('P2')
    assert {related.id for related in c.goals.todos.related_problems(row)} == {'P1', 'P2'}
    c.goals.todos.apply('P2', ReleaseTodo(
        row.revision, CoreWatcherIncarnation.watcher(), row.held(),
        StrikeReason('The unit stopped answering this order before its checked work was finished.')))
    c.transcripts.external('idle').finish(
        turn_id=available_turn_id, item_id='answer', events=(AssistantTranscript(
            'Ready for work', position='answer', timestamp=time.time()),))
    CommanderHeartbeat.register(c.registry)
    configured_repository(c)
    return c


def test_unreadable_checkout_is_its_units_state_and_the_pass_carries_on(
        checkout_bus: Comms, tmp_path: Path):
    """10:08 on 2026-10-21: one unit's unreadable checkout failed the whole pass for every unit."""
    practice = PracticeCopy(checkout_bus.root, tmp_path / 'practice')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    (root, _measurement) = practice.path, practice.measurement
    c = wire(root)
    rule = c.registry.storage_rule
    checkout = rule.order_checkout(rule.initial_roots('worker'), 'P1')
    (checkout / '.git').write_text('gitdir: /nonexistent/removed\n')
    with LaunchRoot.from_path(root).selected():
        read = WorkBranches(checkout).read_checkout()
        assert isinstance(read, UnreadableCheckoutRead) and read.holds_work
        unreadable, = (state for state in UnitState.current(
            c, 'lead', HeartbeatFrame.read(c, 'lead', ProgressRead()))
                      if isinstance(state, Unreadable))
        assert unreadable.unit == c.registry.require('worker').incarnation
        assert str(checkout) in unreadable.reason
        assert 'not a git repository' in unreadable.line()


def test_order_closed_after_the_frame_read_does_not_fail_the_merge_read(
        checkout_bus: Comms, tmp_path: Path):
    """2026-10-24 03:57: P498 closed between the frame read and the merge read."""
    practice = PracticeCopy(checkout_bus.root, tmp_path / 'practice')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    c = wire(practice.path)
    with LaunchRoot.from_path(practice.path).selected():
        frame = HeartbeatFrame.read(c, 'lead', ProgressRead())
        worker, = (state for state in UnitState.current(c, 'lead', frame)
                   if state.unit == c.registry.require('worker').incarnation)
        assert 'P1' in worker.orders and not isinstance(worker, Unreadable)
        row = c.goals.todos.get('P1')
        c.goals.todos.apply('P1', ReleaseTodo(
            row.revision, CoreWatcherIncarnation.watcher(), row.held(),
            StrikeReason('The commander closed this order after the heartbeat frame was read.')))
        c.goals.todos.record_merges(
            CommanderObservation.read(c, c.registry.require('lead'), ProgressRead).merged)
        assert c.goals.todos.get('P1').holder is None


def test_a_renamed_unit_keeps_its_order_checkouts(tmp_path):
    rule = StorageRule(checkout_base=tmp_path / 'checkouts',
                       scratch_base=tmp_path / 'scratch', volume=tmp_path, floor_gib=0)
    born = Thread('unit-mi', frozenset(), str(tmp_path / 'home'),
                  storage_roots=rule.initial_roots('unit-mi'))
    renamed = replace(born, name='codex-mi')
    checkout = rule.order_checkout(rule.roots_of(renamed), 'P1')
    lockstep = rule.repository_checkout(
        rule.roots_of(renamed), 'P1', RepositoryName('OpenHCSDev', 'toad'))
    assert checkout == tmp_path / 'checkouts' / 'unit-mi--P1'
    assert lockstep.name == 'unit-mi--P1--toad'
    assert rule.owns_present_checkout(renamed, checkout)
    assert not rule.owns_present_checkout(
        replace(born, name='codex-mi', storage_roots=rule.initial_roots('codex-mi')), checkout)


def test_fire_refuses_while_the_holders_checkout_retains_work(checkout_bus: Comms):
    """2026-10-24: fire released P602 from a unit holding nine uncommitted edits, and the
    heartbeat's WORK HELD answered the same fact the other way one minute later."""
    c = checkout_bus
    lead = c.registry.require('lead').incarnation
    rule = c.registry.storage_rule
    checkout = rule.order_checkout(rule.initial_roots('worker'), 'P1')
    reason = StrikeReason('The unit broke the tests its order touched and claimed they passed.')
    (checkout / 'work.py').write_text('# not yet published\n')
    with pytest.raises(TodoError, match=f'worker retains work in {checkout}'):
        c.goals.fire('worker', reason, lead)
    assert {c.goals.todos.get(problem).holder for problem in ('P1', 'P3')} == {'worker'}
    git(checkout, 'commit', '-q', '-am', 'Published before release')
    git(checkout, 'push', '-q', 'origin', 'worker-work')
    released = c.goals.fire('worker', reason, lead)
    assert {todo.id for todo in released} == {'P1', 'P3'}
