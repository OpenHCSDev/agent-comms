"""An order's progress is read from its unit's checkout and PRs; no clock strikes or releases it.
The commander's review timer comes back every window until delivery, and the unit never sees it."""

from agent_comms.bus import Bus
import json
import os
import subprocess
import time


import pytest
from test_todos import order
from source_revision_fixture import SourceRepository

from agent_comms.comms import Comms
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.native_tools import NativeRequestTools
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import ReviewTimerUp
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsTodoCreateTool
from agent_comms.work_branches import WorkBranches
from agent_comms.todos import EditingProgress, NoCommitProgress, OrderProgress, PresentCheckout



def git(checkout, *args):
    return subprocess.run(['git', '-C', str(checkout), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_prior_declarations_do_not_enter_native_request_schemas():
    import json
    catalog = json.dumps(NativeRequestTools.catalog())
    assert 'prior_orders' not in catalog
    assert 'preceding' not in catalog


class EmptySource:
    since = 100.0
    pushes = ()

    def commits(self, *excluded):
        return iter(())

    def published(self):
        return iter(())

    def requests(self, state):
        return []


def unit_checkout(path):
    """A unit's checkout with one commit; the test edits it without committing."""
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    (path / 'owner.py').write_text('one\n')
    git(path, 'add', 'owner.py')
    git(path, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'base')
    return path


def edited(path, name, at):
    (path / name).write_text('fix in progress\n')
    os.utime(path / name, (at, at))


class EditingSource(EmptySource):
    """No commit, push or PR names the order; the holder's checkout is the real one."""

    def __init__(self, worktree):
        self.worktree = PresentCheckout(worktree)

    @property
    def edited_at(self):
        return tuple(sorted(self.worktree.edits(), reverse=True)[:1])


@pytest.mark.parametrize('name', ['owner.py', 'new_module.py'])
def test_a_unit_editing_before_its_first_commit_is_moving_not_stalled(tmp_path, name):
    """20:07:17: P358's holder was fixing a send-back, uncommitted; Core read no commit as a stall."""
    worktree = unit_checkout(tmp_path / 'worker--P1')
    assert OrderProgress.from_source(EditingSource(worktree)) == NoCommitProgress()
    edited(worktree, name, 100.0 + 500)
    progress = OrderProgress.from_source(EditingSource(worktree))
    assert progress == EditingProgress(at=600.0)
    edit = time.strftime('%H:%M:%S', time.localtime(600.0))
    assert progress.line == f'uncommitted edits at {edit}, no commit'


def test_a_deleted_file_is_no_edit_time(tmp_path):
    worktree = unit_checkout(tmp_path / 'worker--P1')
    (worktree / 'owner.py').unlink()
    assert tuple(PresentCheckout(worktree).edits()) == ()


def test_a_submodule_with_a_missing_git_directory_does_not_stop_reading_edits(tmp_path):
    """10:09 on 2026-10-21: one unit checkout whose submodule pointed at a removed git directory
    failed every commander heartbeat pass, and comms_todo_list for every caller, for half an hour.
    Both readers of a unit's uncommitted files ask the one owner."""
    package = unit_checkout(tmp_path / 'package')
    worktree = unit_checkout(tmp_path / 'worker--P1')
    git(worktree, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(package), 'pinned')
    git(worktree, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'pin')
    (worktree / 'pinned' / '.git').write_text('gitdir: ../.git/modules/removed\n')
    assert WorkBranches(worktree).uncommitted() == ()
    edited(worktree, 'owner.py', 700.0)
    assert WorkBranches(worktree).uncommitted() == (worktree / 'owner.py',)
    assert tuple(PresentCheckout(worktree).edits()) == (700.0,)
    assert WorkBranches(worktree).local_work


@pytest.fixture
def held(tmp_path):
    """P1 ordered long ago and held by a running worker whose own checkout has no commit for it."""
    root = tmp_path / 'comms'
    root.mkdir(mode=0o700)
    source = SourceRepository.create(tmp_path / 'source')
    (root / 'storage.json').write_text(json.dumps({'checkout_base': str(tmp_path / 'wt')}))
    comms = Comms(root)
    Bus.install(Bus.path_in(comms.root))
    for name in ('lead', 'worker'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    comms.goals.update_goal('lead', SetGoalAction(
        text='Repair the worker', goal_id='request', order=RequestGoalOrder()))
    ThreadCaller('lead').invoke(CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=order(source), request='request', review_at=RecordedEventTime(1000),
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


def test_a_timer_up_tells_the_commander_to_judge_and_strikes_no_one():
    up = ReviewTimerUp('P1', 'worker', 'order-P1', RecordedEventTime(1000.0))
    clock = time.strftime('%H:%M:%S', time.localtime(1000.0))
    assert up.line() == f'TIMER UP: worker P1 at {clock}, order order-P1 -> read its diff and judge it'
    assert up.began() == 1000.0
