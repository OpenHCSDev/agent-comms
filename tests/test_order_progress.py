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


def test_prior_declarations_do_not_enter_native_request_schemas():
    import json
    catalog = json.dumps(NativeRequestTools.catalog())
    assert 'prior_orders' not in catalog
    assert 'preceding' not in catalog


def test_prior_order_store_carry_preserves_original_rows(commander):
    comms, _ = commander
    original = create(commander, 'P1')
    with comms.goals.todos.writing() as db:
        db.execute(f'ALTER TABLE "{Todo.declared_name}" DROP COLUMN "prior_orders"')
    preview = TodoOrderHistory.carry(comms.root, apply=False)
    assert preview.changed == 2
    applied = TodoOrderHistory.carry(comms.root, apply=True)
    assert applied.changed == 2
    assert comms.goals.todos.get('P1') == original
    assert TodoOrderHistory.carry(comms.root, apply=True).changed == 0


def commit(unit):
    git(unit, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'P1 work')
    return git(unit, 'rev-parse', 'HEAD')[:9]


def read(checkout, pulls=None):
    todo, unit = checkout
    return OrderProgress.read(todo, StorageRule(checkout_base=unit.parent), pulls or Listed())


def test_unfinished_check_keeps_other_overdue_orders_visible(
        checkout, commander, commander_script, monkeypatch):
    import asyncio
    from agent_comms.goals import CheckedCommit, CountCheckResult, OrderCheck
    from agent_comms.child_process import NoCountYetPrinted, SignaledOutcome, TimedOutOutcome
    script, _ = commander_script
    todo, unit = checkout
    commit(unit)
    monkeypatch.setattr(OrderCheck, "timeout", 0.05)
    todo = replace(todo, order=replace(todo.order,
        check=OrderCheck(str(unit), "sleep 0.2; echo 0", CountCheckResult(0))))
    monkeypatch.setattr(script.comms.registry, "storage_rule", StorageRule(checkout_base=unit.parent))
    # The child really exceeds its timeout; partial stdout must never become a count.
    outcome = TimedOutOutcome(SignaledOutcome(9))
    assert isinstance(outcome.printed_count(b"0"), NoCountYetPrinted)
    run = asyncio.run(todo.order.check.run())
    sha = subprocess.check_output(('git', '-C', str(unit), 'rev-parse', 'HEAD'), text=True).strip()
    CheckedCommit(todo.order.check.command, run.outcome, run.stdout.decode()).store(str(unit), sha)
    work = OrderProgress.read(todo, StorageRule(checkout_base=unit.parent), Listed())
    assert work.line.endswith("; no count yet")
    second = create(commander, "P2", deadline=1)
    alarms = OrderOverdue.violations(script.comms, script.commander)
    assert {alarm.problem for alarm in alarms} == {todo.id, second.id}
    with pytest.raises(ValueError, match="no count yet"):
        todo.order.check.result.require(run)


@pytest.mark.parametrize('merged_at', (200.0, 201.0))
def test_reissued_order_rejects_earlier_merge_and_accepts_its_own(
        checkout, commander, merged_at):
    first, unit = checkout
    sha = commit(unit)
    earlier = MergedPullRequest(7, 'fix/p1', sha, merge=sha, merged_at=150.0,
                               updated_at=150.0, orders=(first.order.id,))
    comms, caller = commander
    following = replace(first.order, id='resent-P1', at=RecordedEventTime(200.0))
    CommsOrderTool(owner='worker', order=OrderGoalAction(
        problem='P1', order=following, request='request', deadline=RecordedEventTime(3000),
        commander='lead', expected_revision=first.revision,
        assigner=comms.registry.require('lead').incarnation)).apply(comms, caller)
    current = comms.goals.todos.get('P1')
    assert current.prior_orders == (first.order,)
    assert not isinstance(read((current, unit), Listed(merged=(earlier,))), MergedProgress)
    assert comms.goals.todos.get('P1') == current and current.state.open
    assert tuple(earlier.merged_commits(200.0)) == ()
    later = replace(earlier, number=8, merged_at=merged_at, updated_at=merged_at,
                    orders=(following.id,))
    assert tuple(later.merged_commits(200.0)) == (sha,)
    assert read((current, unit), Listed(merged=(earlier, later))) == MergedProgress(sha)


def test_no_commit_since_the_order_is_explicit(checkout):
    progress = read(checkout)
    assert progress == NoCommitProgress()
    assert progress.line == 'no commit since the order'


def test_uncounted_check_keeps_other_overdue_orders_visible(commander, commander_script, checkout, monkeypatch):
    from agent_comms.child_process import NoIntegerPrinted
    from agent_comms.todos import DistanceReading, UncountedTrend

    script, _ = commander_script
    todo, unit = checkout
    comms, _ = commander
    monkeypatch.setattr(script.comms.registry, 'storage_rule', StorageRule(checkout_base=unit.parent))
    monkeypatch.setattr(PullRequests, 'read', lambda *args: ())
    broken = replace(todo.order, check=replace(todo.order.check, command="printf 'no count here'"))
    with comms.goals.todos.writing() as db:
        replace(todo, order=broken).upsert(db)
    create(commander, 'P2', deadline=1)
    commit(unit)
    broken.check.at(git(unit, 'rev-parse', 'HEAD'), str(unit), unit.parent)

    progress = read((comms.goals.todos.get('P1'), unit))
    assert progress.line.endswith("; check prints no integer: printf 'no count here'")
    alarms = order_alarms(script)
    assert len(alarms) == 2
    assert any(line.startswith('ORDER OVERDUE P1 ') and 'order order-P1' in line for line in alarms)
    assert any(line.startswith('ORDER OVERDUE P2 ') and 'order order-P2' in line for line in alarms)
    missing = NoIntegerPrinted()
    assert missing.distance(1) is missing and missing.plus(3) is missing
    assert DistanceReadings((DistanceReading(1, missing, 'printf missing'),)).trend == UncountedTrend(missing, 'printf missing')
    assert DistanceReadings((DistanceReading(1, missing), DistanceReading(2, CountPrinted(2)))).trend.projection == ''


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
    pulls = Listed(open=(UnmergedPullRequest(12, 'fix/p10', 'f' * 40, orders=('order-P10',)), UnmergedPullRequest(7, 'fix/p1', 'a' * 40,
                                                                              updated_at=400.0, orders=('order-P1',))))
    progress = read(checkout, pulls)
    assert progress == OpenPrProgress(7, 'a' * 9, at=400.0)
    assert progress.line == 'PR #7 open at aaaaaaaaa'


def test_removed_checkout_uses_only_retained_pr_and_present_git_errors_propagate(checkout):
    _, unit = checkout
    shutil.rmtree(unit)
    pulls = Listed(open=(UnmergedPullRequest(7, 'fix/p1', 'a' * 40,
                                    updated_at=400.0, orders=('order-P1',)),))
    progress = read(checkout, pulls)
    assert progress == OpenPrProgress(7, 'a' * 9, at=400.0)
    assert progress.line == 'PR #7 open at aaaaaaaaa'
    assert progress.distance_readings() == DistanceReadings()
    assert read(checkout) == NoCommitProgress()
    # Absence is represented, not permission to hide a real Git error.
    unit.mkdir()
    with pytest.raises(ValueError, match='not a git repository'):
        read(checkout, pulls)
    with pytest.raises(ValueError, match='not a git repository'):
        read(checkout)


def test_an_order_owns_the_pr_from_its_identity_whatever_the_branch_says(checkout):
    # #7 was renamed without its P-number; #9 is titled "P1: ..." but comes from another order's branch.
    pulls = Listed(open=(UnmergedPullRequest(9, 'fix/p2', 'b' * 40, updated_at=500.0, orders=('order-P2',)),
                         UnmergedPullRequest(7, 'fix/p1', 'a' * 40, updated_at=400.0, orders=('order-P1',))))
    assert read(checkout, pulls) == OpenPrProgress(7, 'a' * 9, at=400.0)


def test_a_detached_checkout_still_reads_its_order_pr(checkout):
    git(checkout[1], 'switch', '-q', '--detach')
    assert read(checkout, Listed(open=(UnmergedPullRequest(7, 'fix/p1', 'a' * 40, updated_at=400.0, orders=('order-P1',)),))) == OpenPrProgress(7, 'a' * 9, at=400.0)


def test_reused_checkout_replaced_order_and_retired_checkout_keep_problem_progress(
        checkout, commander, tmp_path):
    first, named = checkout
    repository = tmp_path / 'repository'
    named.rename(repository)
    reused = named.parent / 'worker--P41'
    git(repository, 'worktree', 'add', '-q', '-b', 'fix/reused', str(reused))
    sha = commit(reused)
    git(reused, 'push', '-q', 'origin', 'HEAD:fix/reused')
    assert not named.exists()
    assert read((first, reused)).sha == sha
    assert isinstance(read((first, reused)), PushedProgress)

    comms, caller = commander
    replacement = replace(order(repository, 'replacement-P1'), at=RecordedEventTime(2000))
    CommsOrderTool(owner='worker', order=OrderGoalAction(
        problem='P1', order=replacement, request='request',
        deadline=RecordedEventTime(3000), commander='lead', expected_revision=first.revision,
        assigner=comms.registry.require('lead').incarnation)).apply(comms, caller)
    current = comms.goals.todos.get('P1')
    assert current.held_order_ids == ('order-P1', 'replacement-P1')
    assert current.prior_orders == (first.order,)
    assert FieldCodec.decode(Todo, FieldCodec.encode(current)) == current

    bodies = (first.order.pull_request_body('Original working PR'), 'Problem: P1\n')
    for body in bodies:
        request = UnmergedListedPullRequest(7, 'unrelated-branch-name', 'a' * 40,
            updated='1970-01-01T00:00:50Z', body=body, base='main').parsed()
        foreign = replace(request, number=8, orders=('order-P10',), problems=('P10',))
        progress = read((current, reused), Listed(open=(foreign, request)))
        assert isinstance(progress, OpenPrProgress) and progress.number == 7
        assert progress.line == 'PR #7 open at aaaaaaaaa'

    git(repository, 'worktree', 'remove', str(reused))
    published = read((current, reused))
    assert isinstance(published, PushedProgress) and published.sha == sha
    assert not reused.exists()


def test_a_pr_merged_before_the_order_is_not_its_merge(checkout):
    pulls = Listed(merged=(MergedPullRequest(3, 'fix/p1', 'b' * 40, merge='c' * 40, merged_at=99.0, updated_at=600.0, orders=('order-P1',)),))
    assert read(checkout, pulls) == NoCommitProgress()


def test_a_merge_not_yet_installed_waits_only_on_install(checkout):
    progress = read(checkout, Listed(merged=(MergedPullRequest(8, 'fix/p1', 'b' * 40, merge='d' * 40, merged_at=500.0, updated_at=500.0, orders=('order-P1',)),)))
    assert progress == MergedProgress('d' * 40)
    assert progress.line == 'merged as ddddddddd, waiting on install'


def test_unread_github_is_explicit(checkout):
    pulls = Listed()
    pulls.states = WorkBranchError('gh: not logged in')
    progress = read(checkout, pulls)
    assert progress == UnreadProgress('gh: not logged in')


def test_progress_reads_only_explicit_check_receipts(checkout, tmp_path):
    todo, unit = checkout
    marker = tmp_path / 'explicit-check-count'
    check = OrderCheck(str(unit), f'printf x >> {shlex.quote(str(marker))}; printf 0',
                       CountCheckResult(0))
    todo = replace(todo, order=replace(todo.order, check=check))
    commit(unit)
    source = MeasuredProgressSource.of(todo, StorageRule(checkout_base=unit.parent), Listed())
    assert not source.readings().readings
    assert not marker.exists()
    check.at(git(unit, 'rev-parse', 'HEAD').strip(), str(unit), unit.parent)
    assert marker.read_text() == 'x'
    assert len(source.readings().readings) == 1
    assert source.readings().readings[0].distance == CountPrinted(todo.order.checked().untouched(()))
    assert marker.read_text() == 'x'


def test_an_existing_broken_checkout_is_not_silently_treated_as_missing(checkout, tmp_path):
    bad = tmp_path / 'not-a-repository' / 'worker--P1'
    bad.mkdir(parents=True)
    with pytest.raises(WorkBranchError, match='not a git repository'):
        ProgressSource.of(checkout[0], StorageRule(checkout_base=bad.parent), Listed())


def test_board_and_overdue_name_a_merge_waiting_on_install(commander, commander_script, monkeypatch, tmp_path):
    script, _ = commander_script
    # worker's P1 checkout, on the branch its merged PR came from.
    monkeypatch.setattr(script.comms.registry, 'storage_rule', StorageRule(checkout_base=tmp_path / 'wt'))
    unit = tmp_path / 'wt' / 'worker--P1'
    unit.mkdir(parents=True)
    git(unit, 'init', '-q', '-b', 'fix/p1')
    create(commander, 'P1', deadline=1)
    merged = (MergedPullRequest(8, 'fix/p1', 'b' * 40, merge='d' * 40, merged_at=500.0, updated_at=500.0, orders=('order-P1',)),)
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order: merged if state == 'merged' else ())
    assert order_alarms(script) == []
    script.write_board()
    assert '| merged as ddddddddd, waiting on install |' in (tmp_path / 'board.md').read_text()


def test_order_due_moves_the_deadline_in_core_and_on_the_record(commander, commander_script, monkeypatch, tmp_path):
    comms, _ = commander
    script, _ = commander_script
    before = create(commander, 'P1', deadline=1)
    monkeypatch.setattr(type(script), 'post', lambda self, recipients, body: None)
    with pytest.raises(SystemExit, match='8\\+ words'):
        script.due('P1', '23:59', 'too short')
    script.due('P1', '23:59', 'the owner widened the order to the rotation line too')
    moved = comms.goals.todos.get('P1')
    assert moved.deadline.clock() == '23:59:00' and moved.revision == before.revision + 1
    assert 'P1 order order-P1 deadline changed): ' in (tmp_path / 'narrative.md').read_text()


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


def test_future_order_has_no_deadline_alarm(commander, commander_script):
    script, _ = commander_script
    create(commander, 'P1', deadline=time.time() + 3600)
    assert order_alarms(script) == []


def test_overdue_remains_current_until_its_deadline_moves(commander, commander_script):
    comms, _ = commander
    script, _ = commander_script
    todo = create(commander, 'P1')
    readings = [order_alarms(script) for _ in range(3)]
    assert all(len(lines) == 1 and lines[0].startswith('ORDER OVERDUE P1 worker, due ')
               for lines in readings)
    with comms.goals.todos.writing() as db:
        replace(todo, deadline=RecordedEventTime(time.time() + 3600)).upsert(db)
    assert order_alarms(script) == []


def test_install_due_reads_the_schedule_time_alone(commander):
    """InstallDue reads the bus's install schedule and nothing else: a bus with no planned
    install is due, and a planned one is due once its time has come."""
    comms, _ = commander

    def due():
        return [rule.line() for rule in InstallDue.violations(comms, 'lead')]

    assert due() == ['INSTALL DUE (no install is planned on this bus) -> the installer rehearses and '
                     'installs main with tools/upgrade_bus.py and posts the result to the commander']
    PlannedInstallDeadline.after('worker', time.time()).plan(comms.root)
    assert due() == []
    PlannedInstallDeadline.after('worker', time.time() - InstallDeadline.cadence).plan(comms.root)
    (line,) = due()
    assert line.startswith('INSTALL DUE (due ') and 'installer worker) -> the installer' in line


def test_merged_and_delivered_orders_wait_on_the_one_install_deadline(commander, commander_script, monkeypatch, tmp_path):
    """An unrecorded merge and a delivered, uninstalled order wait on the fleet's install; an
    installed order does not. Nobody is struck, and a late install is named, not the orders."""
    comms, caller = commander
    script, _ = commander_script
    monkeypatch.setattr(script.comms.registry, 'storage_rule', StorageRule(checkout_base=tmp_path / 'wt'))
    unit = tmp_path / 'wt' / 'worker--P1'
    unit.mkdir(parents=True)
    git(unit, 'init', '-q', '-b', 'fix/p1')
    for pid in ('P1', 'P2', 'P3'):
        create(commander, pid, deadline=1)
    merged = (MergedPullRequest(8, 'fix/p1', 'b' * 40, merge='d' * 40, merged_at=500.0, updated_at=500.0, orders=('order-P1',)),)
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order: merged if state == 'merged' else ())
    for pid in ('P2', 'P3'):
        CommsOrderDoneTool(problem=pid, order_id=comms.goals.todos.get(pid).order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    p1, p2, p3 = (comms.goals.todos.get(pid) for pid in ('P1', 'P2', 'P3'))
    held = (p1, p2, replace(p3, order=p3.order.installed('the check passed', 'p3.png')))
    read = script.progress()
    assert [t.id for t in held for t in t.order.waiting_on_install(t, read)] == ['P1', 'P2']
    assert order_alarms(script) == []


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
    monkeypatch.setattr(script.comms.registry, "storage_rule", storage)
    reused = storage.order_checkout("worker", "P999")
    reused.mkdir(parents=True)
    git(reused, "init", "-q", "-b", branch)
    git(reused, "remote", "add", "origin", f"https://github.com/{todo.repo.to_wire()}.git")
    monkeypatch.chdir(reused)
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
    commander_order.PullRequestVerb(pid=pid, title=subject, body_file=str(body_file)).apply(script)
    assert todo.order.pull_request_ids(bodies[0]) == (todo.order.id,)
    merged = MergedPullRequest(number, branch, "b" * 40, merge="d" * 40,
        merged_at=500, updated_at=500, orders=todo.order.pull_request_ids(bodies[0]))
    foreign = replace(merged, number=number + 1, merge="f" * 40, orders=("another-order",))
    monkeypatch.setattr(PullRequests, "read", lambda self, repo, state, order:
                        (foreign, merged) if state == "merged" else ())
    assert not storage.order_checkout("worker", pid).exists()
    work = OrderProgress.read(todo, storage, PullRequests())
    assert work == MergedProgress("d" * 40)
    script.write_board()
    assert "merged as ddddddddd, waiting on install" in (tmp_path / "board.md").read_text()
    assert order_alarms(script) == []
