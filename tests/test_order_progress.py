"""An order's progress is read from its unit's checkout and PRs beside its deadline; merged work draws no strike."""
import shutil
import subprocess
import shlex
import time

from dataclasses import replace

import pytest

from agent_comms.recorded_time import RecordedEventTime
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import OrderGoalAction
from agent_comms.commander import order as commander_order
from agent_comms.child_process import CountPrinted
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.tools import CommsOrderDoneTool, CommsOrderTool
from agent_comms.native_tools import NativeRequestTools
from cutover.carry_todo_order_history import TodoOrderHistory
from agent_comms.todos import (CommittedProgress, DistanceReadings, MergedProgress, NoCommitProgress,
    InstallDeadline, InstallDue, OpenPrProgress, OperationDelivery, OrderProgress, PlannedInstallDeadline,
    MeasuredProgressSource, MergedPullRequest, ProgressPoint, ProgressSource, PullRequest, PullRequests,
    PushedProgress, Todo, UnmergedListedPullRequest, UnmergedPullRequest, UnreadProgress)

from agent_comms.standing_rules import OrderOverdue
from agent_comms.storage_rules import StorageRule
from agent_comms.work_branches import WorkBranchError
from test_commander_problem_orders import create, order, working


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


def commit(unit):
    git(unit, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'P1 work')
    return git(unit, 'rev-parse', 'HEAD')[:9]


def read(checkout, pulls=None):
    todo, unit = checkout
    return OrderProgress.read(todo, StorageRule(checkout_base=unit.parent), pulls or Listed())


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


MOVED = time.strftime('%H:%M', time.localtime(1000.0))


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
        assert progress.stalled(100.0, now) is (stalls_at is not None and now > stalls_at)
        assert progress.untouched(now) is (untouched_at is not None and now > untouched_at)


@pytest.mark.parametrize('progress, line, stalls_at', [
    (NoCommitProgress(), 'no commit since the order', 700),
    (UnreadProgress('gh failed'), 'progress unread: gh failed', None),
])
def test_unfound_progress_says_from_its_class_whether_it_stalls(progress, line, stalls_at):
    assert progress.line == line
    for now in (700, 701, 10 ** 9):
        assert progress.stalled(100.0, now) is (stalls_at is not None and now > stalls_at)
        assert not progress.untouched(now)


@pytest.mark.parametrize('progress, now, stalled', [
    (NoCommitProgress(), 100 + 600, False),
    (NoCommitProgress(), 100 + 601, True),
    (CommittedProgress('a' * 9, at=1000.0), 1000 + 900, False),
    (PushedProgress('a' * 9, at=1000.0), 1000 + 901, True),
    (OpenPrProgress(7, 'a' * 9, at=1000.0), 1000 + 901, True),
    (MergedProgress('d' * 9), 10 ** 9, False),
])
def test_work_that_stops_moving_stalls(progress, now, stalled):
    assert progress.stalled(100.0, now) is stalled


def order_alarms(script):
    return [rule.line() for rule in OrderOverdue.violations(script.comms, script.commander)]


