"""An order's progress is read from its unit's checkout and PRs; no clock strikes or releases it.
The commander's review timer comes back every window until delivery, and the unit never sees it."""


from agent_comms.repo import Checkout
import json
import os
import subprocess
import time
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.commander_hud import CommanderFrame
from agent_comms.comms import wire
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.launch_runtime import LaunchRoot
from agent_comms.order_tools import CommsTodoCreateTool
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import GitCommandError
from agent_comms.repository_name import RepositoryName
from agent_comms.threads import Thread
from agent_comms.todos import (
    EditingProgress,
    NoCommitProgress,
    OrderProgress,
    PresentCheckout,
    ProgressRead,
    ProgressSource,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tool_declaration import NativeRequestTools
from agent_comms.ui_model.unit_state import Unreadable
from agent_comms.work_branches import WorkBranches
from fixture_git import IDENTITY
from source_revision_fixture import SourceRepository
from test_todos import order

pytest_plugins = ('test_holder_standing_rules',)



def git(checkout, *args):
    return subprocess.run(['git', *IDENTITY, '-C', str(checkout), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_prior_declarations_do_not_enter_native_request_schemas():
    import json
    catalog = json.dumps(NativeRequestTools.catalog())
    assert 'prior_orders' not in catalog
    assert 'preceding' not in catalog


def unit_checkout(path):
    """A unit's checkout with one commit; the test edits it without committing."""
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    (path / 'owner.py').write_text('one\n')
    git(path, 'add', 'owner.py')
    git(path, 'commit', '-q', '-m', 'base')
    return path


def edited(path, name, at):
    (path / name).write_text('fix in progress\n')
    os.utime(path / name, (at, at))


@pytest.mark.parametrize('name', ['owner.py', 'new_module.py'])
def test_a_unit_editing_before_its_first_commit_is_moving_not_stalled(held, name):
    """20:07:17: P358's holder was fixing a send-back, uncommitted; Core read no commit as a stall."""
    comms, worktree = held
    checkout = PresentCheckout(worktree)
    source = ProgressSource(comms.goals.todos.get('P1'), ProgressRead(()), checkout,
                            worktrees=(checkout,))
    assert OrderProgress.from_source(source) == NoCommitProgress()
    edited(worktree, name, 100.0 + 500)
    checkout = PresentCheckout(worktree)
    progress = OrderProgress.from_source(replace(source, checkout=checkout, worktrees=(checkout,)))
    assert progress == EditingProgress(at=600.0)
    edit = time.strftime('%H:%M:%S', time.localtime(600.0))
    assert progress.line == f'uncommitted edits at {edit}, no commit'


def test_a_deleted_file_is_no_edit_time(tmp_path):
    worktree = unit_checkout(tmp_path / 'worker--P1')
    (worktree / 'owner.py').unlink()
    assert tuple(PresentCheckout(worktree).edits) == ()


def test_a_submodule_with_a_missing_git_directory_does_not_stop_reading_edits(tmp_path):
    """10:09 on 2026-10-21: one unit checkout whose submodule pointed at a removed git directory
    failed every commander heartbeat pass, and comms_todo_list for every caller, for half an hour.
    Both readers of a unit's uncommitted files ask the one owner."""
    package = unit_checkout(tmp_path / 'package')
    worktree = unit_checkout(tmp_path / 'worker--P1')
    git(worktree, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(package), 'pinned')
    git(worktree, 'commit', '-q', '-m', 'pin')
    (worktree / 'pinned' / '.git').write_text('gitdir: ../.git/modules/removed\n')
    assert WorkBranches(worktree).uncommitted() == ()
    edited(worktree, 'owner.py', 700.0)
    assert WorkBranches(worktree).uncommitted() == (worktree / 'owner.py',)
    assert tuple(PresentCheckout(worktree).edits) == (700.0,)
    assert WorkBranches(worktree).local_work


@pytest.fixture
def held(tmp_path):
    """P1 ordered long ago and held by a running worker whose own checkout has no commit for it."""
    root = tmp_path / 'comms'
    root.mkdir(mode=0o700)
    source = SourceRepository.create(tmp_path / 'source')
    (root / 'storage.json').write_text(json.dumps({'checkout_base': str(tmp_path / 'wt')}))
    comms = wire(root)
    for name in ('lead', 'worker'):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path)))
    request = comms.goals.todos.add_request(
        comms.registry.require('lead').incarnation, 'Repair the worker').id
    ThreadCaller('lead').invoke(CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=order(source), request=request, review_at=RecordedEventTime(1000),
        commander='lead', assigner=comms.registry.require('lead').incarnation,
        repo=RepositoryName.from_wire('org/repo'), text='Repair the worker')), comms)
    return comms, unit_checkout(tmp_path / 'wt' / 'worker--P1')


def test_no_clock_takes_a_held_order(held):
    """20:07:17 and 20:24:57: Core struck P358's holder for ten minutes without a commit and
    gave the order away. Nothing reads the clock to strike or release work now."""
    comms, worktree = held
    assert comms.goals.todos.reassignable() == ()
    edited(worktree, 'owner.py', time.time())
    comms.registry.unregister('worker')
    assert comms.goals.todos.reassignable() == ()


def test_the_review_timer_comes_back_every_window_until_delivery(held):
    comms, _ = held
    todo = comms.goals.todos.get('P1')
    first, window = 1000.0, todo.review_window
    assert window == first - todo.order.checked().at.recorded_at('An order')
    assert todo.timer_up(first - 1) == ()
    assert todo.timer_up(first) == (RecordedEventTime(first),)
    assert todo.timer_up(first + window - 1) == (RecordedEventTime(first),)
    assert todo.timer_up(first + 2 * window + 1) == (RecordedEventTime(first + 2 * window),)
    assert todo.reissued_review(5000.0) == RecordedEventTime(5000.0 + window)


def test_deleted_branch_marks_one_card_unreadable(held_bus, installed_runtime):
    """Delete after unit reads, before HUD rendering, then complete another heartbeat."""
    comms = held_bus
    checkout = comms.goals.todos.get('P1').order.checked().check.directory
    branch = 'work/worker--P1'
    git(checkout, 'switch', '-c', branch)
    lead = replace(comms.registry.require('lead'),
                   process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.register(lead)
    heartbeat = CommanderHeartbeat()

    class DeletingRead(ProgressRead):
        def progress(self, held, registry):
            # This happens after UnitState.read guarded the checkout, but before
            # CommanderHud consumes its commit clock. No production code is patched.
            git(checkout, 'update-ref', '-d', f'refs/heads/{branch}')
            return super().progress(held, registry)

    with LaunchRoot.from_path(comms.root).selected():
        before = CommanderObservation.read(comms, lead)
        healthy = tuple(card for card in before.states if card.unit.name != 'worker')
        interrupted = CommanderObservation.read(comms, lead, DeletingRead)
        heartbeat.pass_body(comms, (interrupted,))
        assert CommanderFrame.read_all(comms.root) == (interrupted.hud,)
        assert branch not in WorkBranches(checkout).worktrees()
        reader = WorkBranches(checkout)
        with pytest.raises(GitCommandError) as git_failure:
            reader.difference(reader.origin_main)
        for _ in range(2):
            observed = heartbeat.observe(comms)
            (frame,) = observed
            (broken,) = (card for card in frame.states if card.unit.name == 'worker')
            assert isinstance(broken, Unreadable)
            assert broken.reason == str(git_failure.value)
            assert tuple(card for card in frame.states if card.unit.name != 'worker') == healthy
            heartbeat.pass_body(comms, observed)
            assert CommanderFrame.read_all(comms.root) == (frame.hud,)
        print(broken.line())


def test_push_times_come_from_origin_reflogs_only(tmp_path):
    """The connector walked every reflog (`--all`, 3.2 s on the live checkout) to keep the origin ones."""
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    worktree = unit_checkout(tmp_path / 'worker--P1')
    assert PresentCheckout(worktree).origin_pushes == ()
    git(worktree, 'remote', 'add', 'origin', str(origin))
    git(worktree, 'push', '-q', 'origin', 'HEAD:refs/heads/work')
    pushed = git(worktree, 'rev-parse', 'HEAD')
    git(worktree, 'commit', '-q', '--allow-empty', '-m', 'local only')
    git(worktree, 'push', '-q', 'origin', 'HEAD:refs/heads/side')
    git(worktree, 'commit', '-q', '--allow-empty', '-m', 'never pushed')
    side = git(worktree, 'rev-parse', 'HEAD~1')
    assert sorted(sha for sha, _ in PresentCheckout(worktree).origin_pushes) == sorted([pushed, side])
