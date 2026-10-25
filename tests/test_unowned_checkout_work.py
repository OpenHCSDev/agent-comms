"""Retained Git work protects linked rows through the real watcher and heartbeat."""


from agent_comms.bus import Bus

import json
import os
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from cli_command import core
from order_scope_fixture import FIX_SCOPE
from agent_comms.change_report import FileLine
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import ParkedThreadWorkState, ReadyThreadWorkState
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
)
from agent_comms.commander_pings import CommanderUpdate, RulePing
from agent_comms.commander_hud import CommanderHud
from agent_comms.commander.vibe_check import OverloadDegradation
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
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import (
    RepeatedTodoChange, ServicePassFailing, StaleTodoDecision, StandingRule,
)
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.errors import TodoConflict, TodoError
from agent_comms.todos import (
    PendingProblemOrder, ProgressRead, ReleaseTodo, StrikeReason, StrikeTodo, Todo,
    VoidStrikeTodo,
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


@pytest.fixture
def struck_unowned_bus(checkout_bus: Comms) -> tuple[Comms, StrikeTodo]:
    c = checkout_bus
    row = c.goals.todos.get('P1')
    strike = StrikeTodo(
        row.revision, c.registry.require('lead').incarnation, row.held(),
        'The worker claimed completion without delivering any of its checked implementation.',
        evidence=(row.order.checked().owner,))
    struck = c.goals.todos.apply(row.id, strike)
    c.goals.todos.apply(row.id, ReleaseTodo(
        struck.revision, CoreWatcherIncarnation.watcher(), struck.held(),
        StrikeReason('The worker stopped answering before the checked work was finished.')))
    return c, strike


def run_heartbeat_rules(c: Comms, rules: tuple[StandingRule, ...]) -> None:
    lead = c.registry.require('lead')
    hud = CommanderHud(lead.incarnation, (), (), OverloadDegradation.from_orders((), 0, 0))
    CommanderHeartbeat().pass_body(c, (CommanderObservation(lead, (), rules, (), hud),))


def test_a_concurrent_unstrike_does_not_end_the_heartbeat_pass(struck_unowned_bus):
    c, strike = struck_unowned_bus
    with LaunchRoot.from_path(c.root).selected():
        stale, = (rule for rule in UnownedWheelSpin.violations(c, 'lead')
                  if rule.todo.id == 'P1')
        current = c.goals.todos.get('P1')
        fresh = c.goals.todos.apply(current.id, VoidStrikeTodo(
            current.revision, c.registry.require('lead').incarnation, strike,
            'The cited failure was incorrect and the commander takes back this strike.'))
        following = ServicePassFailing('following-rule', time.time(), 'independent source')
        run_heartbeat_rules(c, (stale, following))

        assigned = c.goals.todos.get('P1')
        assert assigned.holder == 'idle'
        assert assigned.revision == fresh.revision + 1
        assert not assigned.live_strikes('worker')
        assert stale.redecided(c, 'lead', assigned) == ()
        update = CommanderUpdate.latest(c, c.registry.require('lead').incarnation)
        assert update is not None and not update.not_applied
        assert RulePing.capture(following) in update.handled
        assert c.registry.require('lead').incarnation in (
            ServicePassStore.at(c.root).read().commander_completed)


def test_a_non_refreshable_todo_conflict_does_not_end_the_heartbeat_pass(checkout_bus):
    c = checkout_bus

    @dataclass(frozen=True)
    class ConflictingServicePass(ServicePassFailing, declared_name=None):
        current: Todo

        def enforce(self, comms: Comms, commander: str) -> None:
            raise TodoConflict(self.current)

    current = c.goals.todos.get('P1')
    stale = ConflictingServicePass('stale-rule', time.time(), 'changed row', current)
    following = ServicePassFailing('following-rule', time.time(), 'independent source')
    with LaunchRoot.from_path(c.root).selected():
        run_heartbeat_rules(c, (stale, following))
        update = CommanderUpdate.latest(c, c.registry.require('lead').incarnation)
        assert update is not None
        assert update.not_applied == (stale.rejected_line(StaleTodoDecision(current)),)
        assert RulePing.capture(stale) not in update.handled
        assert RulePing.capture(following) in update.handled
        assert c.registry.require('lead').incarnation in (
            ServicePassStore.at(c.root).read().commander_completed)


def test_a_second_todo_conflict_is_reported_without_ending_the_pass(struck_unowned_bus):
    c, strike = struck_unowned_bus

    @dataclass(frozen=True, eq=False)
    class ChangingRow(UnownedWheelSpin, declared_name=None):
        next_row: Todo

        def enforce(self, comms: Comms, commander: str) -> None:
            raise TodoConflict(self.todo)

        def redecided(self, comms: Comms, commander: str,
                      current: Todo) -> tuple[StandingRule, ...]:
            return (type(self)(self.next_row, self.next_row),)

    previous = c.goals.todos.get('P1')
    current = c.goals.todos.apply(previous.id, VoidStrikeTodo(
        previous.revision, c.registry.require('lead').incarnation, strike,
        'The cited failure was incorrect and the commander takes back this strike.'))
    stale = ChangingRow(previous, current)
    following = ServicePassFailing('following-rule', time.time(), 'independent source')
    with LaunchRoot.from_path(c.root).selected():
        rejected = StandingRule.enforce_all(c, 'lead', (stale, following))
        error, = rejected.values()
        with pytest.raises(RepeatedTodoChange) as caught:
            raise error
        assert caught.value.previous == previous
        assert caught.value.current == current
        run_heartbeat_rules(c, (stale, following))
        update = CommanderUpdate.latest(c, c.registry.require('lead').incarnation)
        assert update is not None
        assert update.not_applied == (stale.rejected_line(caught.value),)
        assert RulePing.capture(stale) not in update.handled
        assert RulePing.capture(following) in update.handled


def assert_retained(c: Comms) -> None:
    before = c.goals.todos.get('P2')
    rules = UnownedWheelSpin.violations(c, 'lead')
    assert len(rules) == 1
    held = rules[0]
    assert isinstance(held, CheckoutHeldWheelSpin)
    assert held.work.owner.name == 'worker'
    rule = c.registry.storage_rule
    assert held.work.checkout == rule.order_checkout(rule.initial_roots('worker'), 'P1')
    assert 'worker' in held.line() and str(held.work.checkout) in held.line()
    # The real automatic caller must not bypass the heartbeat's protection.
    UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
    assert c.goals.todos.get('P2') == before
    assert before.holder is None


def test_dirty_then_unpushed_then_pushed_linked_checkout(checkout_bus: Comms, tmp_path: Path):
    original = checkout_bus
    untouched = original.goals.todos.get('P2')
    idle_goal = original.registry.require('idle').goal
    rule = original.registry.storage_rule
    checkout = rule.order_checkout(rule.initial_roots('worker'), 'P1')
    practice = PracticeCopy(original.root, tmp_path / 'practice')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    (root, _measurement) = practice.path, practice.measurement
    c = wire(root)
    def beat():
        frame = CommanderObservation.read(c, c.registry.require('lead'))
        CommanderHeartbeat().pass_body(c, (frame,))

    # The copy retires the source root's processes; the idle unit is live here only while bound here.
    with (LaunchRoot.from_path(root).selected(),
          c.registry.external_process('idle', ProcessIdentity.capture(os.getpid()))):
        (checkout / 'work.py').write_text('# not yet published\n')
        assert_retained(c)
        beat()
        git(checkout, 'add', 'work.py')
        git(checkout, 'commit', '-q', '-m', 'Unpushed work')
        assert git(checkout, 'status', '--porcelain') == ''
        assert_retained(c)
        git(checkout, 'push', '-q', 'origin', 'worker-work')
        c.registry.set_work_state('idle', ParkedThreadWorkState())
        waiting = UnownedWheelSpin.violations(c, 'lead')
        assert len(waiting) == 1
        assert isinstance(waiting[0], WaitingUnownedWheelSpin)
        UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
        assert c.goals.todos.get('P2').holder is None
        beat()
        c.registry.set_work_state('idle', ReadyThreadWorkState())
        available = UnownedWheelSpin.violations(c, 'lead')
        assert len(available) == 1
        assert isinstance(available[0], ReassignedWheelSpin)
        assert available[0].unit == 'idle'
        previous = CommanderUpdate.latest(c, c.registry.require('lead').incarnation)
        assert previous is not None
        assert set(map(RulePing.capture, available)).isdisjoint(previous.handled)
        UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
        assigned = c.goals.todos.get('P2')
        assert assigned.holder == 'idle'
        assert c.registry.require('idle').goal.require().text
        beat()
    history = core('--root', str(root), 'history',
                   '@commander-heartbeat', '--as', 'lead', '--limit', '10')
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
    practice = PracticeCopy(checkout_bus.root, tmp_path / 'practice')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    (root, _measurement) = practice.path, practice.measurement
    c = wire(root)
    rule = c.registry.storage_rule
    checkout = rule.order_checkout(rule.initial_roots('worker'), 'P1')
    (checkout / '.git').write_text('gitdir: /nonexistent/removed\n')
    before = c.goals.todos.get('P2')
    with LaunchRoot.from_path(root).selected():
        read = WorkBranches(checkout).read_checkout()
        assert isinstance(read, UnreadableCheckoutRead) and read.holds_work
        UnownedOrderWatch().watch_pass(lambda: nullcontext(c))
        assert c.goals.todos.get('P2') == before
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
