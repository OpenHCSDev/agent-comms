"""An order's progress is read from its unit's checkout and PRs beside its deadline; merged work draws no strike."""
import json
import os
import subprocess
import time


import pytest
from test_todos import order

from agent_comms.change_report import FileLine, MissingSourceLine
from agent_comms.comms import Comms
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.native_tools import NativeRequestTools
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsTodoCreateTool
from agent_comms.todos import (CommittedProgress, DistanceReadings, EditingProgress, MergedProgress,
    NoCommitProgress, OpenPrProgress, OrderProgress, MergedPullRequest, PresentCheckout, ProgressPoint,
    PullRequest, PullRequests, PushedProgress, UnmergedPullRequest, UnreadProgress)



class Listed(PullRequests):
    """GitHub as a test states it: the pull requests of each state."""

    def __init__(self, **listed: tuple[PullRequest, ...]) -> None:
        super().__init__()
        self.states = listed

    def read(self, repo, state, order):
        if isinstance(self.states, Exception):
            raise self.states
        return self.states.get(state, ())


def git(checkout, *args):
    return subprocess.run(['git', '-C', str(checkout), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_prior_declarations_do_not_enter_native_request_schemas():
    import json
    catalog = json.dumps(NativeRequestTools.catalog())
    assert 'prior_orders' not in catalog
    assert 'preceding' not in catalog


class HeldSource:
    """A source holding an unpushed commit, a pushed commit, an open PR and a merged PR."""
    since = 100.0
    pushes = ()

    def commits(self, *excluded):
        yield ProgressPoint('a' * 40, 1000.0)

    def published(self):
        yield ProgressPoint('e' * 40, 1000.0)

    def readings(self):
        return DistanceReadings()

    def requests(self, state):
        return {'open': [UnmergedPullRequest(7, 'fix/p1', 'b' * 40, updated_at=1000.0, orders=('order-P1',))],
                'merged': [MergedPullRequest(8, 'fix/p1', 'f' * 40, merge='c' * 40, merged_at=200.0, orders=('order-P1',))]}[state]


class EmptySource:
    since = 100.0
    pushes = ()

    def commits(self, *excluded):
        return iter(())

    def published(self):
        return iter(())

    def requests(self, state):
        return []


MOVED = time.strftime('%H:%M:%S', time.localtime(1000.0))


@pytest.mark.parametrize('member, line, stalls_at, untouched_at', [
    (CommittedProgress, f'committed aaaaaaaaa at {MOVED}, not pushed', 1900, None),
    (PushedProgress, f'pushed eeeeeeeee at {MOVED}, no PR', 1900, None),
    (OpenPrProgress, 'PR #7 open at bbbbbbbbb', 1900, 1300),
    (MergedProgress, 'merged as ccccccccc, waiting on install', None, None),
])
def test_each_found_member_says_from_its_class_whether_it_stalls_and_is_untouched(
        member, line, stalls_at, untouched_at):
    (progress,) = member.found(HeldSource())
    assert tuple(member.found(EmptySource())) == ()
    assert progress.line == line
    for now in (1300, 1301, 1900, 1901, 10 ** 9):
        assert progress.stalled(100.0, now) is (stalls_at is not None and now >= stalls_at)
        assert progress.untouched(now) is (untouched_at is not None and now > untouched_at)


@pytest.mark.parametrize('progress, line, stalls_at', [
    (NoCommitProgress(), 'no commit since the order', 700),
    (UnreadProgress('gh failed'), 'progress unread: gh failed', None),
])
def test_unfound_progress_says_from_its_class_whether_it_stalls(progress, line, stalls_at):
    assert progress.line == line
    for now in (700, 701, 10 ** 9):
        assert progress.stalled(100.0, now) is (stalls_at is not None and now >= stalls_at)
        assert not progress.untouched(now)


@pytest.mark.parametrize('progress, now, stalled', [
    (NoCommitProgress(), 100 + 599, False),
    (NoCommitProgress(), 100 + 600, True),
    (CommittedProgress('a' * 9, at=1000.0), 1000 + 899, False),
    (PushedProgress('a' * 9, at=1000.0), 1000 + 901, True),
    (OpenPrProgress(7, 'a' * 9, at=1000.0), 1000 + 901, True),
    (MergedProgress('d' * 9), 10 ** 9, False),
])
def test_work_that_stops_moving_stalls(progress, now, stalled):
    assert progress.stalled(100.0, now) is stalled




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
    assert not progress.stalled(100.0, 100 + 600)
    assert progress.stalled(100.0, 600 + 900)


def test_a_deleted_file_is_no_edit_time(tmp_path):
    worktree = unit_checkout(tmp_path / 'worker--P1')
    (worktree / 'owner.py').unlink()
    assert tuple(PresentCheckout(worktree).edits()) == ()


@pytest.fixture
def held(tmp_path):
    """P1 ordered long ago and held by a running worker whose own checkout has no commit for it."""
    root = tmp_path / 'comms'
    root.mkdir(mode=0o700)
    (root / 'storage.json').write_text(json.dumps({'checkout_base': str(tmp_path / 'wt')}))
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    for name in ('lead', 'worker'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    comms.goals.update_goal('lead', SetGoalAction(
        text='Repair the worker', goal_id='request', order=RequestGoalOrder()))
    ThreadCaller('lead').invoke(CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=order(tmp_path), request='request', review_at=RecordedEventTime(1000),
        commander='lead', repo=RepositoryName.from_wire('org/repo'), text='Repair the worker')), comms)
    return comms, unit_checkout(tmp_path / 'wt' / 'worker--P1')


def reassigned(comms):
    return tuple(todo.id for todo in comms.goals.todos.reassignable(comms.registry))


def test_the_stall_watcher_takes_only_work_that_stopped_moving(held):
    comms, worktree = held
    assert reassigned(comms) == ('P1',)
    edited(worktree, 'owner.py', time.time())
    assert reassigned(comms) == ()


def test_the_stall_watcher_never_takes_work_from_a_stopped_holder(held):
    comms, _ = held
    comms.registry.unregister('worker')
    assert not comms.registry.snapshot().receiving('worker')
    assert reassigned(comms) == ()


def test_a_cited_line_that_does_not_exist_is_a_refusal(tmp_path):
    (tmp_path / 'owner.py').write_text('one\n')
    FileLine('owner.py', 1).require_exists(tmp_path)
    for missing in (FileLine('owner.py', 2), FileLine('absent.py', 1)):
        with pytest.raises(MissingSourceLine, match='does not exist'):
            missing.require_exists(tmp_path)
