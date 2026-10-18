"""An order's progress is read from its unit's checkout and PRs beside its deadline; merged work draws no strike."""
import subprocess
import time

import pytest

from agent_comms.todos import (CommittedProgress, MergedProgress, NoCommitProgress, OpenPrProgress, OrderProgress,
    PullRequest, PullRequests, PushedProgress, UnreadProgress)

from agent_comms.messages import InfoKind
from agent_comms.storage_rules import StorageRule
from test_commander_problem_orders import create


class Listed(PullRequests):
    """GitHub as a test states it: the pull requests of each state."""

    def __init__(self, **listed: tuple[PullRequest, ...]) -> None:
        super().__init__()
        self.states = listed

    def read(self, repo, state):
        if isinstance(self.states, Exception):
            raise self.states
        return self.states.get(state, ())


def git(checkout, *args):
    return subprocess.run(['git', '-C', str(checkout), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(commander, tmp_path):
    """worker's checkout of P1, its one commit already on origin's default branch."""
    origin, unit = tmp_path / 'origin.git', tmp_path / 'wt' / 'worker--P1'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    unit.mkdir(parents=True)
    for command in (('init', '-q', '-b', 'main'), ('remote', 'add', 'origin', str(origin)),
                    ('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base'),
                    ('push', '-q', 'origin', 'main'), ('fetch', '-q', 'origin'),
                    ('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')):
        git(unit, *command)
    return create(commander, 'P1'), unit


def commit(unit):
    git(unit, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'P1 work')
    return git(unit, 'rev-parse', 'HEAD')[:9]


def read(checkout, pulls=None):
    todo, unit = checkout
    return OrderProgress.read(todo, StorageRule(checkout_base=unit.parent), pulls or Listed())


def test_no_commit_since_the_order_draws_a_strike(checkout):
    progress = read(checkout)
    assert progress == NoCommitProgress() and progress.strikes
    assert progress.line == 'no commit since the order'


def test_an_unpushed_commit_names_its_sha_and_time(checkout):
    sha = commit(checkout[1])
    progress = read(checkout)
    assert type(progress) is CommittedProgress and progress.sha == sha and progress.strikes
    assert progress.line == f"committed {sha} at {time.strftime('%H:%M', time.localtime(progress.at))}, not pushed"


def test_a_pushed_commit_with_no_pr_says_so(checkout):
    sha = commit(checkout[1])
    git(checkout[1], 'push', '-q', 'origin', 'HEAD:fix/p1')
    progress = read(checkout)
    assert type(progress) is PushedProgress and progress.sha == sha and progress.line.endswith(', no PR')


def test_an_open_pr_naming_the_order_shows_its_number_and_head(checkout):
    pulls = Listed(open=(PullRequest(12, 'Fix (P10)', 'f' * 40), PullRequest(7, 'Fix the thing (P1)', 'a' * 40,
                                                                              updated_at=400.0)))
    progress = read(checkout, pulls)
    assert progress == OpenPrProgress(7, 'a' * 9, 400.0) and progress.strikes
    assert progress.line == 'PR #7 open at aaaaaaaaa'


def test_an_open_pr_untouched_since_the_order_belongs_to_an_earlier_one(checkout):
    assert read(checkout, Listed(open=(PullRequest(5, 'P1: old', 'e' * 40, updated_at=99.0),))) == NoCommitProgress()


def test_a_pr_merged_before_the_order_is_not_its_merge(checkout):
    pulls = Listed(merged=(PullRequest(3, 'P1: earlier order', 'b' * 40, 'c' * 40, 99.0, 600.0),))
    assert read(checkout, pulls) == NoCommitProgress()


def test_a_merged_order_records_done_and_draws_no_strike(checkout):
    progress = read(checkout, Listed(merged=(PullRequest(8, 'P1: fix', 'b' * 40, 'd' * 40, 500.0, 500.0),)))
    assert progress == MergedProgress('d' * 9) and not progress.strikes
    assert progress.line == 'merged as ddddddddd, record done'


def test_unread_github_is_said_and_still_draws_a_strike(checkout):
    pulls = Listed()
    pulls.states = ValueError('gh: not logged in')
    progress = read(checkout, pulls)
    assert progress == UnreadProgress('gh: not logged in') and progress.strikes


def test_board_and_overdue_print_a_merged_order_as_done_not_late(commander, commander_script, monkeypatch, tmp_path):
    script, _ = commander_script
    create(commander, 'P1', deadline=1)
    merged = (PullRequest(8, 'P1: fix', 'b' * 40, 'd' * 40, 500.0, 500.0),)
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state: merged if state == 'merged' else ())
    assert script.deadline_lines(script.open_todos()) == [
        f'DUE PASSED P1 worker (due {script.hhmm(script.COMMS.goals.todos.get("P1").deadline)}): '
        'merged as ddddddddd, record done']
    script.board()
    assert '| merged as ddddddddd, record done |' in (tmp_path / 'board.md').read_text()


def test_order_due_moves_the_deadline_in_core_and_on_the_record(commander, commander_script, monkeypatch, tmp_path):
    comms, _ = commander
    script, _ = commander_script
    before = create(commander, 'P1', deadline=1)
    monkeypatch.setattr(script, 'post', lambda recipients, body: None)
    with pytest.raises(SystemExit, match='8\\+ words'):
        script.due('P1', '23:59', 'too short')
    script.due('P1', '23:59', 'the owner widened the order to the rotation line too')
    moved = comms.goals.todos.get('P1')
    assert script.hhmm(moved.deadline) == '23:59' and moved.revision == before.revision + 1
    assert 'P1 order order-P1 due ' in (tmp_path / 'narrative.md').read_text()


@pytest.mark.parametrize('progress, now, stalled', [
    (NoCommitProgress(), 100 + 600, False),
    (NoCommitProgress(), 100 + 601, True),
    (CommittedProgress('a' * 9, 1000.0), 1000 + 900, False),
    (PushedProgress('a' * 9, 1000.0), 1000 + 901, True),
    (OpenPrProgress(7, 'a' * 9, 1000.0), 1000 + 901, True),
    (MergedProgress('d' * 9), 10 ** 9, False),
])
def test_work_that_stops_moving_stalls(progress, now, stalled):
    assert progress.stalled(100.0, now) is stalled


def test_a_stalled_order_prints_stall_with_its_progress(commander, commander_script):
    script, _ = commander_script
    create(commander, 'P1', deadline=time.time() + 3600)
    assert script.deadline_lines(script.open_todos()) == [
        'STALL P1 worker: no commit since the order -> push it now or reassign']


def test_overdue_is_announced_once_per_crossing(commander, commander_script):
    comms, _ = commander
    script, _ = commander_script
    create(commander, 'P1')  # ordered at 100 and due at 1000; it stalled at 700, before the watch
    polls, since = [], 800.0
    for _ in range(3):
        polls, since = polls + script.deadline_lines(script.open_todos(), since), time.time()
    assert len(polls) == 1 and polls[0].startswith('OVERDUE: worker (due ')
    comms.messaging.send_message('worker', 'lead', 'on it', InfoKind)
    replied = script.deadline_lines(script.open_todos(), since)
    assert len(replied) == 1 and '; REPLIED ' in replied[0] and 'on it' in replied[0]
    assert script.deadline_lines(script.open_todos(), time.time()) == []
