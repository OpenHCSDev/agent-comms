"""An order's progress is read from its holder's checkout and PRs; no clock strikes or releases it.
The colleague's review timer comes back every window until delivery, and the holder never sees it."""


import asyncio
import json
import os
import subprocess
import time
from contextlib import nullcontext
from dataclasses import replace

import pytest

import fixture_git
from agent_comms.comms import wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.launch_runtime import LaunchRoot
from agent_comms.planning.goal_actions import CreateProblemGoalAction
from agent_comms.planning.orders import (
    EditingProgress,
    NoCommitProgress,
    OrderProgress,
    PresentCheckout,
    ProgressRead,
    ProgressSource,
)
from agent_comms.projects.checkouts import (
    Checkout,
    CommitsNaming,
    EveryCommit,
)
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.work_branches import WorkBranchError, WorkBranches
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.team_hud import TeamFrame
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.orders import CommsTodoCreateTool
from agent_comms.ui_model.teammate_state import Unreadable
from source_revision_fixture import SourceRepository
from team_heartbeat_fixture import ObservedHeartbeat, current_hud, observe_team
from test_todos import order

pytest_plugins = ('test_holder_standing_rules',)



git = fixture_git.run


def holder_checkout(path):
    """A holder's checkout with one commit; the test edits it without committing."""
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
def test_a_holder_editing_before_its_first_commit_is_moving_not_stalled(held, name):
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
    worktree = holder_checkout(tmp_path / 'worker--P1')
    (worktree / 'owner.py').unlink()
    assert tuple(PresentCheckout(worktree).edits) == ()


def test_a_submodule_with_a_missing_git_directory_does_not_stop_reading_edits(tmp_path):
    """10:09 on 2026-10-21: one holder checkout whose submodule pointed at a removed git directory
    failed every colleague heartbeat pass, and comms_todo_list for every caller, for half an hour.
    Both readers of a holder's uncommitted files ask the one owner."""
    package = holder_checkout(tmp_path / 'package')
    worktree = holder_checkout(tmp_path / 'worker--P1')
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
    for name in ('colleague', 'worker'):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path)))
    request = comms.goals.todos.add_request(
        comms.registry.require('colleague').incarnation, 'Repair the worker').id
    ThreadCaller('colleague').invoke(CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=order(source), request=request, review_at=RecordedEventTime(1000), assigner=comms.registry.require('colleague').incarnation,
        repo=RepositoryName.from_wire('org/repo'), text='Repair the worker')), comms)
    return comms, holder_checkout(tmp_path / 'wt' / 'worker--P1')


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


def test_deleted_branch_marks_one_card_unreadable(held_bus):
    """Delete after the holder reads, before HUD rendering, then complete another heartbeat."""
    comms = held_bus
    checkout = comms.goals.todos.get('P1').order.checked().check.directory
    branch = 'work/worker--P1'
    git(checkout, 'switch', '-c', branch)
    colleague = replace(comms.registry.require('colleague'),
                   process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.register(colleague)

    with LaunchRoot.from_path(comms.root).selected():
        before = observe_team(comms, ProgressRead())
        healthy = tuple(card for card in before.states if card.unit.name != 'worker')
        git(checkout, 'update-ref', '-d', f'refs/heads/{branch}')
        interrupted = observe_team(comms, ProgressRead())
        asyncio.run(ObservedHeartbeat(interrupted).run(lambda: nullcontext(comms)))
        published, = TeamFrame.read_all(comms.root)
        assert tuple(card.unit for card in published.units) == tuple(card.unit for card in interrupted.states)
        assert branch not in WorkBranches(checkout).worktrees()
        for _ in range(2):
            frame = observe_team(comms, ProgressRead())
            (broken,) = (card for card in frame.states if card.unit.name == 'worker')
            assert isinstance(broken, Unreadable)
            with pytest.raises((GitCommandError, WorkBranchError)) as source_failure:
                broken.source.require_read()
            assert broken.reason == str(source_failure.value)
            assert tuple(card for card in frame.states if card.unit.name != 'worker') == healthy
            asyncio.run(ObservedHeartbeat(frame).run(lambda: nullcontext(comms)))
            published, = TeamFrame.read_all(comms.root)
            assert published == current_hud(comms, colleague.name, frame.frame, frame.states,
                                            published.usage)


def test_push_times_come_from_origin_reflogs_only(tmp_path):
    """The connector walked every reflog (`--all`, 3.2 s on the live checkout) to keep the origin ones."""
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    worktree = holder_checkout(tmp_path / 'worker--P1')
    assert PresentCheckout(worktree).origin_pushes == ()
    git(worktree, 'remote', 'add', 'origin', str(origin))
    git(worktree, 'push', '-q', 'origin', 'HEAD:refs/heads/work')
    pushed = git(worktree, 'rev-parse', 'HEAD')
    git(worktree, 'commit', '-q', '--allow-empty', '-m', 'local only')
    git(worktree, 'push', '-q', 'origin', 'HEAD:refs/heads/side')
    git(worktree, 'commit', '-q', '--allow-empty', '-m', 'never pushed')
    side = git(worktree, 'rev-parse', 'HEAD~1')
    assert sorted(sha for sha, _ in PresentCheckout(worktree).origin_pushes) == sorted([pushed, side])


def test_every_order_reads_the_newest_match_git_grep_gives(tmp_path):
    """Each held order re-walked every origin ref with its own `git log -1 --grep`: about 500
    walks of one history per heartbeat (36 s of a 220 s pass, 2026-10-30). One walk per
    selection answers every order with the newest match Git's --grep would give."""
    worktree = holder_checkout(tmp_path / 'worker--P1')
    for message in ('P1 first', 'fix P12 slot', 'body only\n\nrefs p3 here', 'P1: second', 'XP1 is not P1x'):
        git(worktree, 'commit', '-q', '--allow-empty', '-m', message)
    checkout = PresentCheckout(worktree)
    # The capture resolves the named refs it read (for-each-ref), as callers pass them; not HEAD.
    refs = (git(worktree, 'symbolic-ref', 'HEAD').strip(),)
    for problem in ('P1', 'P12', 'P3', 'P4'):
        expected = git(worktree, 'log', '-1', '--extended-regexp', '--regexp-ignore-case',
                       f'--grep=(^|[^A-Za-z0-9_]){problem}([^A-Za-z0-9_]|$)', '--format=%H', *refs, '--not')
        assert [point.sha for point in checkout.commits(CommitsNaming(problem), refs)] == expected.split()
    newest = git(worktree, 'rev-parse', 'HEAD')
    assert [point.sha for point in checkout.commits(EveryCommit(), refs)] == newest.split()
