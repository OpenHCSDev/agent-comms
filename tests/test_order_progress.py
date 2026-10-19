"""An order's progress is read from its unit's checkout and PRs beside its deadline; merged work draws no strike."""
import subprocess
import time

from dataclasses import replace

import pytest

from agent_comms.recorded_time import RecordedEventTime
from agent_comms.tools import CommsOrderDoneTool
from agent_comms.todos import (CommittedProgress, DistanceReadings, MergedProgress, NoCommitProgress,
    OpenPrProgress, OperationDelivery, OrderProgress, PlannedInstallDeadline, PullRequest,
    PullRequests, PushedProgress, UnreadProgress)

from agent_comms.messages import InfoKind
from agent_comms.storage_rules import StorageRule
from test_commander_problem_orders import create, working


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


@pytest.fixture
def checkout(commander, tmp_path):
    """worker's checkout of P1 on branch fix/p1, its one commit already on origin's default branch."""
    origin, unit = tmp_path / 'origin.git', tmp_path / 'wt' / 'worker--P1'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    unit.mkdir(parents=True)
    for command in (('init', '-q', '-b', 'main'), ('remote', 'add', 'origin', str(origin)),
                    ('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base'),
                    ('push', '-q', 'origin', 'main'), ('fetch', '-q', 'origin'),
                    ('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'),
                    ('switch', '-q', '-c', 'fix/p1')):
        git(unit, *command)
    return create(commander, 'P1'), unit


def commit(unit):
    git(unit, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'P1 work')
    return git(unit, 'rev-parse', 'HEAD')[:9]


def read(checkout, pulls=None):
    todo, unit = checkout
    return OrderProgress.read(todo, StorageRule(checkout_base=unit.parent), pulls or Listed())


def test_no_commit_since_the_order_is_explicit(checkout):
    progress = read(checkout)
    assert progress == NoCommitProgress()
    assert progress.line == 'no commit since the order'


def test_an_unpushed_commit_names_its_sha_and_time(checkout):
    sha = commit(checkout[1])
    progress = read(checkout)
    assert type(progress) is CommittedProgress and progress.sha == sha
    assert progress.line == f"committed {sha} at {time.strftime('%H:%M', time.localtime(progress.at))}, not pushed"


def test_a_pushed_commit_with_no_pr_says_so(checkout):
    sha = commit(checkout[1])
    git(checkout[1], 'push', '-q', 'origin', 'HEAD:fix/p1')
    progress = read(checkout)
    assert type(progress) is PushedProgress and progress.sha == sha and progress.line.endswith(', no PR')


def test_an_open_pr_naming_the_order_shows_its_number_and_head(checkout):
    pulls = Listed(open=(PullRequest(12, 'fix/p10', 'f' * 40, orders=('order-P10',)), PullRequest(7, 'fix/p1', 'a' * 40,
                                                                              updated_at=400.0, orders=('order-P1',))))
    progress = read(checkout, pulls)
    assert progress == OpenPrProgress(7, 'a' * 9, at=400.0)
    assert progress.line == 'PR #7 open at aaaaaaaaa'


def test_an_order_owns_the_pr_from_its_identity_whatever_the_branch_says(checkout):
    # #7 was renamed without its P-number; #9 is titled "P1: ..." but comes from another order's branch.
    pulls = Listed(open=(PullRequest(9, 'fix/p2', 'b' * 40, updated_at=500.0, orders=('order-P2',)),
                         PullRequest(7, 'fix/p1', 'a' * 40, updated_at=400.0, orders=('order-P1',))))
    assert read(checkout, pulls) == OpenPrProgress(7, 'a' * 9, at=400.0)


def test_a_detached_checkout_still_reads_its_order_pr(checkout):
    git(checkout[1], 'switch', '-q', '--detach')
    assert read(checkout, Listed(open=(PullRequest(7, 'fix/p1', 'a' * 40, updated_at=400.0, orders=('order-P1',)),))) == OpenPrProgress(7, 'a' * 9, at=400.0)


def test_an_open_pr_untouched_since_the_order_belongs_to_an_earlier_one(checkout):
    assert read(checkout, Listed(open=(PullRequest(5, 'fix/p1', 'e' * 40, updated_at=99.0, orders=('order-P1',)),))) == NoCommitProgress()


def test_a_pr_merged_before_the_order_is_not_its_merge(checkout):
    pulls = Listed(merged=(PullRequest(3, 'fix/p1', 'b' * 40, 'c' * 40, 99.0, 600.0, orders=('order-P1',)),))
    assert read(checkout, pulls) == NoCommitProgress()


def test_a_merged_order_still_awaits_its_record(checkout):
    progress = read(checkout, Listed(merged=(PullRequest(8, 'fix/p1', 'b' * 40, 'd' * 40, 500.0, 500.0, orders=('order-P1',)),)))
    assert progress == MergedProgress('d' * 9)
    assert progress.line == 'merged as ddddddddd, awaiting record'


def test_unread_github_is_explicit(checkout):
    pulls = Listed()
    pulls.states = ValueError('gh: not logged in')
    progress = read(checkout, pulls)
    assert progress == UnreadProgress('gh: not logged in')


def test_board_and_overdue_name_a_merge_awaiting_its_record(commander, commander_script, monkeypatch, tmp_path):
    script, _ = commander_script
    # worker's P1 checkout, on the branch its merged PR came from.
    monkeypatch.setattr(script.COMMS.registry, 'storage_rule', StorageRule(checkout_base=tmp_path / 'wt'))
    unit = tmp_path / 'wt' / 'worker--P1'
    unit.mkdir(parents=True)
    git(unit, 'init', '-q', '-b', 'fix/p1')
    create(commander, 'P1', deadline=1)
    merged = (PullRequest(8, 'fix/p1', 'b' * 40, 'd' * 40, 500.0, 500.0, orders=('order-P1',)),)
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order: merged if state == 'merged' else ())
    assert "awaiting record" in deadline_lines(script)[0]
    script.board()
    assert '| merged as ddddddddd, awaiting record |' in (tmp_path / 'board.md').read_text()


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


class HeldSource:
    """A source holding an unpushed commit, a pushed commit, an open PR and a merged PR."""
    since = 100.0

    def commits(self, *excluded):
        commits = {('--remotes',): ('a' * 40, 1000.0), ('origin/HEAD',): ('e' * 40, 1000.0)}
        yield commits[excluded]

    def readings(self):
        return DistanceReadings()

    def requests(self, state):
        return {'open': [PullRequest(7, 'fix/p1', 'b' * 40, updated_at=1000.0, orders=('order-P1',))],
                'merged': [PullRequest(8, 'fix/p1', 'f' * 40, merge='c' * 40, merged_at=200.0, orders=('order-P1',))]}[state]


class EmptySource:
    since = 100.0

    def commits(self, *excluded):
        return iter(())

    def requests(self, state):
        return []


MOVED = time.strftime('%H:%M', time.localtime(1000.0))


@pytest.mark.parametrize('member, line, stalls_at, untouched_at', [
    (CommittedProgress, f'committed aaaaaaaaa at {MOVED}, not pushed', 1900, None),
    (PushedProgress, f'pushed eeeeeeeee at {MOVED}, no PR', 1900, None),
    (OpenPrProgress, 'PR #7 open at bbbbbbbbb', 1900, 1300),
    (MergedProgress, 'merged as ccccccccc, awaiting record', None, None),
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


def deadline_lines(script, since=0.0):
    """One watch tick's deadline lines: the pending orders and their progress, read once."""
    pending = [t for t in script.held_orders() if t.pending]
    return script.deadline_lines(pending, script.progress(), since)


def test_a_stalled_order_prints_stall_with_its_progress(commander, commander_script):
    script, _ = commander_script
    create(commander, 'P1', deadline=time.time() + 3600)
    assert deadline_lines(script) == [
        'STALL P1 worker: no commit since the order -> push it now or reassign']


def test_overdue_remains_announced_until_recorded(commander, commander_script):
    comms, _ = commander
    script, _ = commander_script
    create(commander, 'P1')  # ordered at 100 and due at 1000; it stalled at 700, before the watch
    polls, since = [], 800.0
    for _ in range(3):
        polls, since = polls + deadline_lines(script, since), time.time()
    assert len(polls) == 3 and all(line.startswith('OVERDUE: worker (due ') for line in polls)
    comms.messaging.send('worker', 'lead', 'on it', InfoKind).message
    replied = deadline_lines(script, since)
    assert len(replied) == 1 and '; REPLIED ' in replied[0] and 'on it' in replied[0]
    assert deadline_lines(script, time.time())[0].startswith("OVERDUE:")


def test_merged_and_delivered_orders_wait_on_the_one_install_deadline(commander, commander_script, monkeypatch, tmp_path):
    """An unrecorded merge and a delivered, uninstalled order wait on the fleet's install; an
    installed order does not. Nobody is struck, and a late install is named, not the orders."""
    comms, caller = commander
    script, _ = commander_script
    monkeypatch.setattr(script.COMMS.registry, 'storage_rule', StorageRule(checkout_base=tmp_path / 'wt'))
    unit = tmp_path / 'wt' / 'worker--P1'
    unit.mkdir(parents=True)
    git(unit, 'init', '-q', '-b', 'fix/p1')
    for pid in ('P1', 'P2', 'P3'):
        create(commander, pid, deadline=1)
    merged = (PullRequest(8, 'fix/p1', 'b' * 40, 'd' * 40, 500.0, 500.0, orders=('order-P1',)),)
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order:
                        merged if (state, order.id) == ('merged', 'order-P1') else ())
    for pid in ('P2', 'P3'):
        CommsOrderDoneTool(problem=pid, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    p1, p2, p3 = (comms.goals.todos.get(pid) for pid in ('P1', 'P2', 'P3'))
    held = (p1, p2, replace(p3, order=p3.order.installed('the check passed', 'p3.png')))
    read = script.progress()
    assert script.install_lines(held, read, 1000.0) == ['INSTALL not planned; waiting: P1 worker, P2 worker']
    PlannedInstallDeadline(RecordedEventTime(2000.0), 'worker').plan(comms.root)
    due = time.strftime('%H:%M', time.localtime(2000.0))
    assert script.install_lines(held, read, 1000.0) == [f'INSTALL due {due}; waiting: P1 worker, P2 worker']
    assert script.install_lines(held, read, 2001.0) == [
        f'INSTALL LATE (due {due}), not the orders; waiting: P1 worker, P2 worker']
    assert script.install_lines(held[2:], read, 2001.0) == []
    assert "awaiting record" in deadline_lines(script)[0]


@pytest.mark.parametrize("pid,branch,number,subject", [
    ("P77", "docs/module-layers", 1581, "P77: module layer map"),
    ("P109", "ratchet/none-absence", 1624, "NoneAbsence: declared measure"),
    ("P117", "ratchet/decoder-marker", 1629, "Boundary decoders declare themselves"),
])
def test_unrecorded_merge_uses_order_identity_after_checkout_is_removed(
        commander, commander_script, monkeypatch, tmp_path, pid, branch, number, subject):
    script, _ = commander_script
    todo = create(commander, pid, deadline=1)
    storage = StorageRule(checkout_base=tmp_path / "wt")
    monkeypatch.setattr(script.COMMS.registry, "storage_rule", storage)
    body_file = tmp_path / "pr-body.md"
    body_file.write_text(subject)
    bodies = []
    original = subprocess.run

    def forge(command, **arguments):
        if tuple(command[:3]) == ("gh", "pr", "create"):
            bodies.append(arguments["input"])
            return subprocess.CompletedProcess(command, 0, f"https://github.com/test/repo/pull/{number}", "")
        return original(command, **arguments)

    monkeypatch.setattr(subprocess, "run", forge)
    script.PullRequestVerb(pid=pid, title=subject, body_file=str(body_file)).run()
    assert todo.order.pull_request_ids(bodies[0]) == (todo.order.id,)
    stamp = "1970-01-01T00:08:20Z"
    merged = PullRequest.listed(number, branch, "b" * 40, "d" * 40, stamp, stamp, bodies[0])
    foreign = replace(merged, number=number + 1, merge="f" * 40, orders=("another-order",))
    monkeypatch.setattr(PullRequests, "read", lambda self, repo, state, order:
                        (foreign, merged) if state == "merged" else ())
    assert not storage.order_checkout("worker", pid).exists()
    work = OrderProgress.read(todo, storage, PullRequests())
    assert work == MergedProgress("d" * 9)
    script.board()
    assert "merged as ddddddddd, awaiting record" in (tmp_path / "board.md").read_text()
    for state in (work, PushedProgress("b" * 9, at=500.0), NoCommitProgress()):
        alarms = script.deadline_lines([todo], {pid: state}, time.time() + 100)
        assert alarms[0].startswith("OVERDUE:") and state.line in alarms[0]
