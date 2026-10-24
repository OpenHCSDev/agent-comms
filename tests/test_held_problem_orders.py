"""Problem ids select held orders through real commander commands on copied state."""
from contextlib import chdir
from dataclasses import replace
import json
import shlex
import time

import pytest

from test_todos import order, thread

from agent_comms.commander.order import OrderFile, main
from agent_comms.commander.request import RequestAddVerb
from agent_comms.field_codec import FieldCodec, GoalId
from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    DeliveredProblemOrder, MergedListedPullRequest, OrderMessage, PullRequestCommit,
    PullRequestLink,
)
from agent_comms.work_branches import WorkBranches
from agent_comms.owner_requests import NewestOpenRequest
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsChannelsTool


pytest_plugins = ("test_todos",)


@pytest.fixture
def cited_source(pr_commander, proof_build_checkouts):
    """The source the proof fixture built. The commander declares its repos as plain Repo,
    and the practice copy decodes them as such, so the source is selected by GitHub name."""
    return next(graph.repository for graph in proof_build_checkouts
                if graph.repository.github == pr_commander.repos[0].github)


@pytest.fixture
def held_commander(pr_commander, cited_source):
    initial_order_goal_id = GoalId.mint()
    source = pr_commander
    worker = thread("worker", source.root, 2.0)
    source.comms.registry.register(worker)
    RequestAddVerb(text="Repair the copied order workflow without changing original retained state").run(source)
    lead = source.comms.registry.require(source.commander)
    todo = source.comms.goals.todos.create(
        "P1", source.repos[0].github, "Repair the held workflow", creator=lead, assigner=lead.incarnation,
        owner=worker, generation=initial_order_goal_id,
        request=NewestOpenRequest().for_order(source.comms.goals.todos, lead.incarnation),
        review_at=RecordedEventTime(time.time() + 1000), order=order(cited_source),
    )
    published = source.comms.messaging.send(source.commander, worker.name, "Repair the held workflow through its existing declaration.")
    source.comms.goals.todos.apply(todo.id, OrderMessage(todo.revision, lead.incarnation, published.message.message_id))
    original = source.comms.goals.todos.get(todo.id)
    practice = PracticeCopy(source.root, source.root.parent / f"{source.root.name}-copied")
    practice.include(tuple(RootFile.existing_in(practice.root)))
    (root, _) = practice.path, practice.measurement
    ctx = source.model_copy(update={"root": root, "state": root / "state"})
    yield ctx
    assert source.comms.goals.todos.get(todo.id) == original


def link_pull_request(ctx, pid, number):
    """The order's recorded PR link, as `order pr` writes it."""
    store = ctx.comms.goals.todos
    current = store.get(pid)
    order = replace(current.order, pull_requests=(PullRequestLink(current.repo, number),))
    with store.writing() as connection:
        replace(current, order=order, revision=current.revision + 1).upsert(connection)
    return store.get(pid)


def merge_two_commit_fix(source, binaries, number):
    """A fix PR merged with a merge commit: its first commit only half-fixes count.txt.

    Returns (base, first, merge); GitHub's row for the request joins the forge stub."""
    checkout = source.checkout
    source.git(checkout, 'fetch', '-q', 'origin', WorkBranches.main)
    source.git(checkout, 'checkout', '-q', '-B', 'main', f'origin/{WorkBranches.main}')
    (checkout / 'count.txt').write_text('2\n')
    source.git(checkout, 'add', 'count.txt')
    source.git(checkout, 'commit', '-qm', 'The defect counts two')
    base = source.git(checkout, 'rev-parse', 'HEAD').strip()
    source.git(checkout, 'checkout', '-q', '-b', f'fix/{number}')
    commits = []
    for count in (1, 0):
        (checkout / 'count.txt').write_text(f'{count}\n')
        source.git(checkout, 'commit', '-qam', f'The fix leaves {count}')
        commits.append(source.git(checkout, 'rev-parse', 'HEAD').strip())
    source.git(checkout, 'checkout', '-q', 'main')
    source.git(checkout, 'merge', '-q', '--no-ff', '-m', f'Merge pull request #{number}', f'fix/{number}')
    merge = source.git(checkout, 'rev-parse', 'HEAD').strip()
    source.git(checkout, 'push', '-q', 'origin', 'main', f'{commits[-1]}:refs/pull/{number}/head')
    from github_fixture import GitHubFixture

    GitHubFixture(binaries).put(source.github,
        MergedListedPullRequest(number=number, branch=f'fix/{number}', head=commits[-1],
                                updated='2026-10-23T12:00:00Z', base=WorkBranches.main,
                                reviewable_times=('2026-10-23T12:00:00Z',),
                                merge=PullRequestCommit(merge), merged='2026-10-23T12:00:00Z'),
        body=f'## fix {number}')
    return base, commits[0], merge


def test_clean_resend_derives_the_existing_order_and_leaves_no_strike(held_commander, cited_source):
    ctx = held_commander
    original = ctx.comms.goals.todos.get("P1")
    marker = ctx.root / "unfinished"
    marker.touch()
    now = time.time()
    replacement = replace(
        order(cited_source), id="replacement-order",
        check=OrderCheck(ctx.repos[0].checkout, f"test ! -e {shlex.quote(str(marker))}"),
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120),
    )
    document = OrderFile(
        replacement,
        "The copied workflow still contains an unfinished decision that prevents completion",
        "This is stupid because a stale decision prevents the existing owner from finishing",
        unplanned_reason="This isolated workflow verifies held order selection rather than advancing the production plan.",
        problems=("P1",),
    )
    path = ctx.root / "order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    main(["send", "worker", str(path), "--clean"], ctx)
    saved = ctx.comms.goals.todos.get("P1")
    assert saved.order.id != original.order.id
    assert saved.messages[-1].message_id != original.messages[-1].message_id
    assert saved.strikes == original.strikes == ()
    assert ctx.comms.registry.require("worker").goal.require().id == saved.held().generation
    ThreadCaller("worker").invoke(CommsChannelsTool(), ctx.comms)


def test_second_unplanned_order_is_refused_without_recording(held_commander, cited_source):
    second_workflow_goal_id = GoalId.mint()
    ctx = held_commander
    original = ctx.comms.goals.todos.get("P1")
    marker = ctx.root / "unfinished"
    marker.touch()
    document = OrderFile(
        replace(order(cited_source), id="second-order",
                check=OrderCheck(ctx.repos[0].checkout, f"test ! -e {shlex.quote(str(marker))}")),
        "The copied workflow still contains an unfinished decision that prevents completion",
        "This is stupid because assigning another order hides the already unfinished work",
        unplanned_reason="This isolated workflow verifies refusal rather than advancing the production plan.",
        problems=("P2",),
    )
    lead = ctx.comms.registry.require(ctx.commander)
    ctx.comms.goals.todos.create(
        "P2", ctx.repos[0].github, "Repair the second workflow", creator=lead, assigner=lead.incarnation, owner=lead,
        generation=second_workflow_goal_id, request=original.request.require(),
        review_at=RecordedEventTime(time.time() + 1000), order=document.order,
    )
    path = ctx.root / "second-order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    before = ctx.comms.goals.todos.for_commander(ctx.incarnation)
    goal = ctx.comms.registry.require("worker").goal
    with pytest.raises(SystemExit) as refused:
        main(["send", "worker", str(path)], ctx)
    assert "worker already holds orders P1; running units holding no order:" in str(refused.value)
    assert ctx.comms.goals.todos.for_commander(ctx.incarnation) == before
    assert ctx.comms.goals.todos.get("P1") == original
    assert ctx.comms.registry.require("worker").goal == goal
    print(str(refused.value))


def test_done_checks_the_merged_tree_and_records_the_first_commit(
    held_commander, cited_source, tmp_path,
):
    """The merged tree is the delivered tree; a half-fixed first commit is still the delivery."""
    ctx = held_commander
    base, first, merge = merge_two_commit_fix(cited_source, tmp_path / 'bin', 4)
    current = ctx.comms.goals.todos.get('P1')
    check = OrderCheck(cited_source.checkout, 'cat count.txt', CountCheckResult(0))
    store = ctx.comms.goals.todos
    with store.writing() as connection:
        replace(current, order=replace(current.order, check=check),
                revision=current.revision + 1).upsert(connection)
    link_pull_request(ctx, 'P1', 4)
    with chdir(cited_source.checkout):
        main(["done", "worker", "P1"], ctx)
    saved = store.get("P1")
    assert not saved.pending
    assert isinstance(saved.order, DeliveredProblemOrder)
    assert (saved.order.delivery.delivered, saved.order.delivery.commit) == (first, merge)
    assert saved.order.check_base(cited_source.checkout) == base


def test_done_without_a_merged_pull_request_records_nothing(held_commander):
    ctx = held_commander
    original = ctx.comms.goals.todos.get("P1")
    with pytest.raises(ValueError, match="P1 has no merged pull request"):
        main(["done", "worker", "P1"], ctx)
    assert ctx.comms.goals.todos.get("P1") == original


def test_a_problem_not_held_by_the_unit_is_refused(held_commander):
    ctx = held_commander
    original = ctx.comms.goals.todos.get("P1")
    with pytest.raises(ValueError, match="worker holds no order on P2"):
        main(["done", "worker", "P2"], ctx)
    with pytest.raises(ValueError, match="other holds no order on P1"):
        ctx.comms.goals.todos.resolve_order(ctx.incarnation, "other", "P1")
    assert ctx.comms.goals.todos.get("P1") == original
