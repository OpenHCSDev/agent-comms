"""Checkout allocation and unpublished-file protection at their storage owner."""


import asyncio
import json
import os
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms, wire
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.errors import TodoError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.turn_item import (
    NativeItemId,
    NativeTurnId,
)
from agent_comms.installer.practice import PracticeCopy
from agent_comms.messaging.bus import Bus
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.goals import OrderCheck
from agent_comms.planning.order_runs import MechanicalOrderRun
from agent_comms.planning.order_words import StrikeReason
from agent_comms.planning.orders import (
    Assignment,
    PendingProblemOrder,
    ProgressRead,
    ReleaseTodo,
    StrikeTodo,
    Todo,
    TodoConflict,
    VoidStrikeTodo,
)
from agent_comms.planning.problem_findings import ExpectedProblem
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.checkouts import (
    Checkout,
    RepositoryReads,
)
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.projects.work_branches import UnreadableCheckoutRead, WorkBranches
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.storage.locked_store import RootFile
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.team_pings import RulePing, TeamUpdate
from agent_comms.team.standing import UnownedWheelSpin
from agent_comms.team.standing_rules import (
    RepeatedTodoChange,
    ServicePassFailing,
    StaleTodoDecision,
    StandingRule,
)
from agent_comms.team.team_heartbeat import (
    TeamHeartbeat,
    TeamObservation,
)
from agent_comms.transcript.transcript_events import AssistantTranscript
from agent_comms.ui_model.teammate_state import TeammateState, Unreadable
from order_scope_fixture import FIX_SCOPE
from team_heartbeat_fixture import ObservedHeartbeat, capture_frame


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
    assert rule.owns_present_checkout(worker, checkout, RepositoryReads())
    assert rule.owns_checkout(worker, lockstep, RepositoryName('OpenHCSDev', 'toad'), RepositoryReads())
    assert not rule.owns_checkout(worker, misnamed, RepositoryName('OpenHCSDev', 'toad'), RepositoryReads())
    assert rule.problem_checkouts(
        rule.initial_roots('worker'), 'P1') == (checkout, misnamed, lockstep)
    assert lockstep in rule.checkouts_of(worker, RepositoryReads())
    assert misnamed not in rule.checkouts_of(worker, RepositoryReads())


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
    for name in ('colleague', 'worker'):
        c.registry.declare(ExternalThread(
            name, frozenset(), Checkout(home)))
    c.registry.declare(ExternalThread(
        'idle', frozenset(), Checkout(spare),
        process_identity=ProcessIdentity.capture(os.getpid())))
    request = c.goals.todos.add_request(
        c.registry.require('colleague').incarnation, 'Keep changes with their author').id
    colleague, worker = c.registry.require('colleague'), c.registry.require('worker')
    now = time.time()
    order = PendingProblemOrder(
        owner=FileLine('work.py', 1), scope=FIX_SCOPE, run=MechanicalOrderRun(),
        check=OrderCheck(home, 'false'),
        edits=('Keep the changes with their author until they have been pushed.',),
        banned=('No second store of checkout holders.',), report='Report the actual checkout.',
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120))
    for problem in ('P1', 'P2'):
        c.goals.todos.create(
            problem, RepositoryName.from_wire('OpenHCSDev/agent-comms'),
            'Keep changes with their author', creator=colleague, assigner=colleague.incarnation, owner=worker,
            generation=shared_work_goal_id, review_at=RecordedEventTime(now + 3600),
            order=order, request=request)
    # The holder takes both under a new goal: one order, one write.
    c.goals.todos.assign(
        tuple(ExpectedProblem(problem, c.goals.todos.get(problem).revision)
              for problem in ('P1', 'P2')), owner=worker,
        assigner=worker.acting_identity, generation=reissued_work_goal_id,
        review_at=RecordedEventTime(now + 3600), order=order.reissued(now + 1),
        request=request)
    # Core now refuses unrelated work to a unit holding an order; a bus saved before that rule
    # can still hold both, so worker's unrelated P3 is written as that saved row.
    with c.goals.todos.writing() as db:
        Todo.ordered(
            'P3', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
            'Keep unrelated work separate', colleague.incarnation,
            Assignment.of(worker.incarnation, colleague.incarnation, other_work_goal_id),
            RecordedEventTime(now + 3600), order.reissued(now + 2), request,
        ).insert_if_absent(db)
    row = c.goals.todos.get('P2')
    assert {related.id for related in c.goals.todos.related_problems(row)} == {'P1', 'P2'}
    c.goals.todos.apply('P2', ReleaseTodo(
        row.revision, row.held().owner_identity, row.held(),
        StrikeReason('The holder releases this order before its checked work was finished.')))
    c.transcripts.external('idle').finish(
        turn_id=available_turn_id, item_id=NativeItemId('answer'), events=(AssistantTranscript(
            'Ready for work', position=NativeItemId('answer'), timestamp=time.time()),))
    configured_repository(c)
    return c


@pytest.fixture
def struck_unowned_bus(checkout_bus: Comms) -> tuple[Comms, StrikeTodo]:
    c = checkout_bus
    row = c.goals.todos.get('P1')
    strike = StrikeTodo(
        row.revision, c.registry.require('colleague').incarnation, row.held(),
        'The worker claimed completion without delivering any of its checked implementation.',
        evidence=(row.order.checked().owner,))
    struck = c.goals.todos.apply(row.id, strike)
    c.goals.todos.apply(row.id, ReleaseTodo(
        struck.revision, struck.held().owner_identity, struck.held(),
        StrikeReason('The holder releases this order before the checked work was finished.')))
    return c, strike


def run_heartbeat_rules(c: Comms, rules: tuple[StandingRule, ...]) -> None:
    frame = capture_frame(c, ProgressRead())
    observed = TeamObservation(frame, rules, ())
    asyncio.run(ObservedHeartbeat(observed).run(lambda: nullcontext(c)))


def test_a_concurrent_unstrike_does_not_end_the_heartbeat_pass(struck_unowned_bus):
    c, strike = struck_unowned_bus
    with LaunchRoot.from_path(c.root).selected():
        stale, = (rule for rule in UnownedWheelSpin.violations(c)
                  if rule.todo.id == 'P1')
        current = c.goals.todos.get('P1')
        fresh = c.goals.todos.apply(current.id, VoidStrikeTodo(
            current.revision, c.registry.require('colleague').incarnation, strike,
            'The cited failure was incorrect and the teammate takes back this strike.'))
        following = ServicePassFailing(watch='following-rule', since=time.time(), error='independent source',
                                       failed_at=time.time())
        run_heartbeat_rules(c, (stale, following))

        assigned = c.goals.todos.get('P1')
        assert assigned.holder == 'idle'
        assert assigned.revision == fresh.revision + 1
        assert not assigned.strikes_against(c.registry.require('worker').incarnation)
        assert stale.redecided(c, assigned) == ()
        update, = TeamUpdate.latest(c, c.registry.require('colleague').incarnation)
        assert not update.not_applied
        assert RulePing.capture(following) in update.handled
        assert c.registry.require('colleague').incarnation in (
            ServicePassStore.at(c.root).read().teammate_completed)


def test_a_non_refreshable_todo_conflict_does_not_end_the_heartbeat_pass(checkout_bus):
    c = checkout_bus

    @dataclass(frozen=True)
    class ConflictingServicePass(ServicePassFailing):
        current: Todo

        def enforce(self, comms: Comms) -> None:
            raise TodoConflict(self.current)

    current = c.goals.todos.get('P1')
    stale = ConflictingServicePass(watch='stale-rule', since=time.time(), error='changed row',
                                   failed_at=time.time(), current=current)
    following = ServicePassFailing(watch='following-rule', since=time.time(), error='independent source',
                                   failed_at=time.time())
    with LaunchRoot.from_path(c.root).selected():
        run_heartbeat_rules(c, (stale, following))
        update, = TeamUpdate.latest(c, c.registry.require('colleague').incarnation)
        # The rule's line counts seconds since its failure, so the pass's render and this one
        # can differ by a clock tick; the refusal and the rule it refused are the facts.
        refused, = update.not_applied
        assert refused.startswith(f"Not applied ({StaleTodoDecision(current).explanation()}): ")
        assert stale.watch in refused
        assert RulePing.capture(stale) not in update.handled
        assert RulePing.capture(following) in update.handled
        assert c.registry.require('colleague').incarnation in (
            ServicePassStore.at(c.root).read().teammate_completed)


def test_a_second_todo_conflict_is_reported_without_ending_the_pass(struck_unowned_bus):
    c, strike = struck_unowned_bus

    @dataclass(frozen=True, eq=False)
    class ChangingRow(UnownedWheelSpin):
        next_row: Todo

        def enforce(self, comms: Comms) -> None:
            raise TodoConflict(self.todo)

        def redecided(self, comms: Comms, current: Todo) -> tuple[StandingRule, ...]:
            return (type(self)(self.next_row, self.next_row),)

    previous = c.goals.todos.get('P1')
    current = c.goals.todos.apply(previous.id, VoidStrikeTodo(
        previous.revision, c.registry.require('colleague').incarnation, strike,
        'The cited failure was incorrect and the teammate takes back this strike.'))
    stale = ChangingRow(previous, current)
    following = ServicePassFailing(watch='following-rule', since=time.time(), error='independent source',
                                   failed_at=time.time())
    with LaunchRoot.from_path(c.root).selected():
        rejected = StandingRule.enforce_all(c, (stale, following))
        error, = rejected.values()
        with pytest.raises(RepeatedTodoChange) as caught:
            raise error
        assert caught.value.previous == previous
        assert caught.value.current == current
        run_heartbeat_rules(c, (stale, following))
        update, = TeamUpdate.latest(c, c.registry.require('colleague').incarnation)
        assert update.not_applied == (stale.rejected_line(caught.value),)
        assert RulePing.capture(stale) not in update.handled
        assert RulePing.capture(following) in update.handled


def test_unreadable_checkout_is_its_holders_state_and_the_pass_carries_on(
        checkout_bus: Comms, tmp_path: Path):
    """10:08 on 2026-10-21: one holder's unreadable checkout failed the whole pass for every teammate."""
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
        unreadable, = (state for state in TeammateState.current(
            c, capture_frame(c, ProgressRead()))
                      if isinstance(state, Unreadable))
        assert unreadable.unit == c.registry.require('worker').incarnation
        assert str(checkout) in unreadable.reason
        # The line carries the recorded failure; git's own wording varies by version.
        assert unreadable.reason in unreadable.line()


def test_order_closed_after_the_frame_read_does_not_fail_the_merge_read(
        checkout_bus: Comms, tmp_path: Path):
    """2026-10-24 03:57: P498 closed between the frame read and the merge read."""
    practice = PracticeCopy(checkout_bus.root, tmp_path / 'practice')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    c = wire(practice.path)
    with LaunchRoot.from_path(practice.path).selected():
        frame = capture_frame(c, ProgressRead())
        worker, = (state for state in TeammateState.current(c, frame)
                   if state.unit == c.registry.require('worker').incarnation)
        assert 'P1' in worker.orders and not isinstance(worker, Unreadable)
        row = c.goals.todos.get('P1')
        c.goals.todos.apply('P1', ReleaseTodo(
            row.revision, row.held().owner_identity, row.held(),
            StrikeReason('The holder releases this order after the heartbeat frame was read.')))
        asyncio.run(TeamHeartbeat().run(lambda: nullcontext(c)))
        assert c.goals.todos.get('P1').holder is None


def test_a_renamed_holder_keeps_its_order_checkouts(tmp_path):
    rule = StorageRule(checkout_base=tmp_path / 'checkouts',
                       scratch_base=tmp_path / 'scratch', volume=tmp_path, floor_gib=0)
    born = NativeThread('unit-mi', frozenset(), Checkout(tmp_path / 'home'),
                  storage_roots=rule.initial_roots('unit-mi'))
    renamed = replace(born, name='codex-mi')
    checkout = rule.order_checkout(rule.roots_of(renamed), 'P1')
    lockstep = rule.repository_checkout(
        rule.roots_of(renamed), 'P1', RepositoryName('OpenHCSDev', 'toad'))
    assert checkout == tmp_path / 'checkouts' / 'unit-mi--P1'
    assert lockstep.name == 'unit-mi--P1--toad'
    assert rule.owns_present_checkout(renamed, checkout, RepositoryReads())
    assert not rule.owns_present_checkout(
        replace(born, name='codex-mi', storage_roots=rule.initial_roots('codex-mi')), checkout,
        RepositoryReads())


def test_fire_refuses_while_the_holders_checkout_retains_work(checkout_bus: Comms):
    """2026-10-24: fire released P602 from a holder with nine uncommitted edits, and the
    heartbeat's WORK HELD answered the same fact the other way one minute later."""
    c = checkout_bus
    holder = c.registry.require('worker').acting_identity
    rule = c.registry.storage_rule
    checkout = rule.order_checkout(rule.initial_roots('worker'), 'P1')
    reason = StrikeReason('The holder broke the tests its order touched and stops this work.')
    (checkout / 'work.py').write_text('# not yet published\n')
    with pytest.raises(TodoError, match=f'worker retains work in {checkout}'):
        c.goals.fire('worker', reason, holder)
    assert {c.goals.todos.get(problem).holder for problem in ('P1', 'P3')} == {'worker'}
    git(checkout, 'commit', '-q', '-am', 'Published before release')
    git(checkout, 'push', '-q', 'origin', 'worker-work')
    released = c.goals.fire('worker', reason, holder)
    assert {todo.id for todo in released} == {'P1', 'P3'}
