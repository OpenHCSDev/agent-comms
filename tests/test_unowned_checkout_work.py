"""Retained Git work protects linked rows through the real watcher and heartbeat."""

from agent_comms.bus import Bus

import json
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from cli_command import core
from order_scope_fixture import FIX_SCOPE
from agent_comms.change_report import FileLine
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.standing import (
    CheckoutHeldWheelSpin,
    ReassignedWheelSpin,
    UnownedOrderWatch,
    UnownedWheelSpin,
    WaitingUnownedWheelSpin,
)
from agent_comms.commander_heartbeat import (
    CommanderHeartbeat,
    CommanderObservation,
    CommanderReview,
)
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import OrderCheck
from agent_comms.installer.practice import practice_copy
from agent_comms.launch_root import LaunchRoot
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageDenied, StorageRule, shell_storage_effects
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.todos import PendingProblemOrder, ProgressRead, ReleaseTodo, StrikeReason
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.ui_model.unit_state import UnitState, Unreadable
from agent_comms.work_branches import UnreadableCheckoutRead, WorkBranches
from order_scope_fixture import FIX_SCOPE


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ('git', '-C', str(directory), '-c', 'user.name=Checkout fixture',
         '-c', 'user.email=checkout@example.invalid', *arguments),
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout


def test_one_problem_owns_a_checkout_in_each_repository_it_changes(checkout_bus, tmp_path):
    rule = checkout_bus.registry.storage_rule
    worker = checkout_bus.registry.require('worker')
    checkout = rule.order_checkout('worker', 'P1')
    other = tmp_path / 'toad'
    other.mkdir()
    git(other, 'init', '-q', '--initial-branch=main')
    git(other, 'commit', '-q', '--allow-empty', '-m', 'Second repository')
    git(other, 'remote', 'add', 'origin', 'https://github.com/OpenHCSDev/toad.git')
    lockstep = rule.repository_checkout('worker', 'P1', RepositoryName('OpenHCSDev', 'toad'))
    git(other, 'worktree', 'add', '-q', '-b', 'worker-toad', str(lockstep))
    misnamed = checkout.with_name('worker--P1--textual')
    git(other, 'worktree', 'add', '-q', '-b', 'worker-misnamed', str(misnamed))

    assert checkout.name == 'worker--P1'
    assert lockstep.name == 'worker--P1--toad'
    assert rule.owns_checkout(worker, checkout, checkout)
    assert rule.owns_checkout(worker, lockstep, lockstep)
    assert not rule.owns_checkout(worker, misnamed, misnamed)
    assert rule.problem_checkouts('worker', 'P1') == (checkout, misnamed, lockstep)
    assert lockstep in rule.checkouts_of(worker)
    assert misnamed not in rule.checkouts_of(worker)


def test_a_second_repository_checkout_is_judged_by_its_source_before_it_exists(
    checkout_bus, tmp_path,
):
    rule = checkout_bus.registry.storage_rule
    other = tmp_path / 'toad'
    other.mkdir()
    git(other, 'init', '-q', '--initial-branch=main')
    git(other, 'commit', '-q', '--allow-empty', '-m', 'Second repository')
    git(other, 'remote', 'add', 'origin', 'https://github.com/OpenHCSDev/toad.git')
    lockstep = rule.repository_checkout('worker', 'P1', RepositoryName('OpenHCSDev', 'toad'))
    adding = f'git -C {other} worktree add -q -b worker-toad {lockstep}'

    def decide(command: str, cwd: Path) -> None:
        checkout_bus.registry.decide_storage('worker', shell_storage_effects(command, cwd))

    with pytest.raises(StorageDenied, match='is not a checkout of worker'):
        decide(f'git worktree add -q -b worker-wrong {lockstep}', tmp_path / 'declared-project')
    with pytest.raises(StorageDenied, match='is not a checkout of worker'):
        decide(f'git -C {other} worktree add -q {lockstep.with_name("worker--P1--textual")}',
               tmp_path)
    with pytest.raises(StorageDenied, match='is not a checkout of idle'):
        checkout_bus.registry.decide_storage('idle', shell_storage_effects(adding, tmp_path))
    assert not lockstep.exists()
    decide(adding, tmp_path)
    subprocess.run(adding.split(), check=True, capture_output=True, timeout=15)
    assert lockstep in rule.checkouts_of(checkout_bus.registry.require('worker'))


@pytest.fixture
def checkout_bus(tmp_path: Path, configured_repository, installed_runtime) -> Comms:
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
    Bus.install(Bus.path_in(root))
    c = Comms(root)
    Bus.install(Bus.path_in(c.root))
    for name in ('lead', 'worker', 'idle'):
        c.registry.declare(c.registry.new_thread(
            name, frozenset(), str(home), execution=ExternalThreadExecution))
    request = c.goals.todos.add_request(
        c.registry.require('lead').incarnation, 'Keep changes with their author').id
    lead, worker = c.registry.require('lead'), c.registry.require('worker')
    now = time.time()
    order = PendingProblemOrder(
        id='shared-work', owner=FileLine('work.py', 1), scope=FIX_SCOPE, run=MechanicalOrderRun(),
        check=OrderCheck(str(home), 'true'),
        edits=('Keep the changes with their author until they have been pushed.',),
        banned=('No second store of checkout holders.',), report='Report the actual checkout.',
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120))
    for problem in ('P1', 'P2'):
        c.goals.todos.create(
            problem, RepositoryName.from_wire('OpenHCSDev/agent-comms'),
            'Keep changes with their author', creator=lead, assigner=lead.incarnation, owner=worker,
            generation='shared-work', review_at=RecordedEventTime(now + 3600),
            order=order, request=request)
    # Both current IDs change, as they do when Core hands work out again.
    for problem in ('P1', 'P2'):
        row = c.goals.todos.get(problem)
        c.goals.todos.assign(
            problem, expected_revision=row.revision, owner=worker,
            assigner=lead.incarnation, generation='reissued-work',
            review_at=RecordedEventTime(now + 3600), order=order.reissued(now + 1),
            request=request)
    c.goals.todos.create(
        'P3', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Keep unrelated work separate', creator=lead, assigner=lead.incarnation, owner=worker,
        generation='other-work', review_at=RecordedEventTime(now + 3600),
        order=order.reissued(now + 2), request=request)
    row = c.goals.todos.get('P2')
    assert {related.id for related in c.goals.todos.related_problems(row)} == {'P1', 'P2'}
    c.goals.todos.apply('P2', ReleaseTodo(
        row.revision, CoreWatcherIncarnation.watcher(), row.held(),
        StrikeReason('The unit stopped answering this order before its checked work was finished.')))
    c.transcripts.external('idle').finish(
        turn_id='available', item_id='answer', events=(AssistantTranscript(
            'Ready for work', position='answer', timestamp=time.time()),))
    CommanderHeartbeat.register(c.registry)
    configured_repository(c)
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
    UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
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

        def beat():
            frame = CommanderObservation.read(c, c.registry.require('lead'))
            review.wake(c, CommanderHeartbeat.sender, frame.thread, frame.rules, frame.states)

        with LaunchRoot.from_path(root).selected():
            (checkout / 'work.py').write_text('# not yet published\n')
            assert_retained(c)
            beat()
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
            UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
            assert c.goals.todos.get('P2').holder is None
            beat()
            config.write(tmp_path / 'copy-home')
            available = UnownedWheelSpin.violations(c, 'lead')
            assert len(available) == 1
            assert isinstance(available[0], ReassignedWheelSpin)
            assert available[0].unit == 'idle'
            assert set(available).isdisjoint(review.rules)
            UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
            assigned = c.goals.todos.get('P2')
            assert assigned.holder == 'idle'
            assert c.registry.require('idle').goal.require().text
            beat()
        history = core('--root', str(root), 'history',
                       '--target', '@commander-heartbeat', '--as', 'lead', '--limit', '10')
        assert history.returncode == 0, history.stderr
        assert f'WORK HELD: P2 worker retains work in {checkout}' in history.stdout
        assert f'NO LONGER CURRENT: WORK HELD: P2 worker retains work in {checkout}' in history.stdout
        print('PRIVATE HEARTBEAT', history.stdout)
        print('PRIVATE WATCHER: dirty and unpushed retained; clean pushed assigned to', assigned.holder)
    assert original.goals.todos.get('P2') == untouched
    assert original.registry.require('idle').goal == idle_goal


def test_unreadable_checkout_is_its_units_state_and_the_pass_carries_on(
        checkout_bus: Comms, tmp_path: Path):
    """10:08 on 2026-10-21: one unit's unreadable checkout failed the whole pass for every unit."""
    with practice_copy(checkout_bus.root, tmp_path / 'practice') as (root, _measurement):
        c = Comms(root)
        checkout = c.registry.storage_rule.order_checkout('worker', 'P1')
        (checkout / '.git').write_text('gitdir: /nonexistent/removed\n')
        before = c.goals.todos.get('P2')
        with LaunchRoot.from_path(root).selected():
            read = WorkBranches(checkout).read_checkout()
            assert isinstance(read, UnreadableCheckoutRead) and read.holds_work
            UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
            assert c.goals.todos.get('P2') == before
            unreadable, = (state for state in UnitState.current(c, 'lead', ProgressRead())
                          if isinstance(state, Unreadable))
            assert unreadable.unit == c.registry.require('worker').incarnation
            assert str(checkout) in unreadable.reason
            assert 'not a git repository' in unreadable.line()
