"""Problem ids select held orders through real teammate commands on copied state."""
import json
import shlex
import time
from contextlib import chdir
from dataclasses import replace

import pytest

from agent_comms.coordination.errors import Refused
from agent_comms.errors import GoalRefused
from agent_comms.execution.owner_protocol import OwnerFailure, OwnerLine
from agent_comms.installer.practice import PracticeCopy
from agent_comms.nominal.field_codec import FieldCodec, GoalId, OrderId
from agent_comms.planning.goal_actions import (
    ClearGoalAction,
    CreateProblemGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goals import (
    CountCheckResult,
    OrderCheck,
    ProblemGoalOrder,
)
from agent_comms.planning.order_runs import ApplicationOrderRun
from agent_comms.planning.order_words import DeliveryReport
from agent_comms.planning.orders import (
    DeliveredProblemOrder,
    MeasuredReview,
    MergedListedPullRequest,
    OrderMessage,
    SelectedClaimReviewer,
    StrikeTodo,
)
from agent_comms.planning.owner_requests import NewestOpenRequest
from agent_comms.planning.pull_requests import (
    PullRequestCommit,
    PullRequestLink,
)
from agent_comms.planning.todo_refusals import TeammateOccupied
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.storage.locked_store import RootFile
from agent_comms.storage.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.team.order import OrderFile, main
from agent_comms.team.request import RequestAddVerb
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.channels import CommsChannelsTool
from agent_comms.transport.tools.orders import (
    CommsOrderDeliverTool,
    CommsTodoCreateTool,
)
from test_todos import order, thread

pytest_plugins = ("test_todos",)


@pytest.fixture
def cited_source(pr_team, proof_build_checkouts):
    """The source the proof fixture built. The colleague declares its repos as plain Repo,
    and the practice copy decodes them as such, so the source is selected by GitHub name."""
    return next(graph.repository for graph in proof_build_checkouts
                if graph.repository.github == pr_team.repos[0].github)


@pytest.fixture
def held_team(pr_team, cited_source):
    initial_order_goal_id = GoalId.mint()
    source = pr_team
    worker = thread("worker", source.root, 2.0)
    source.comms.registry.register(worker)
    RequestAddVerb(text="Repair the copied order workflow without changing original retained state").run(source)
    colleague = source.comms.registry.require(source.actor_name)
    todo = source.comms.goals.todos.create(
        "P1", source.repos[0].github, "Repair the held workflow", creator=colleague, assigner=colleague.incarnation,
        owner=worker, generation=initial_order_goal_id,
        request=NewestOpenRequest().for_order(source.comms.goals.todos, colleague.incarnation),
        review_at=RecordedEventTime(time.time() + 1000), order=order(cited_source),
    )
    published = source.comms.messaging.send(source.actor_name, worker.name, "Repair the held workflow through its existing declaration.")
    source.comms.goals.todos.apply(todo.id, OrderMessage(todo.revision, colleague.incarnation, published.message.message_id,
                                                      at=RecordedEventTime(time.time())))
    original = source.comms.goals.todos.get(todo.id)
    practice = PracticeCopy(source.root, source.root.parent / f"{source.root.name}-copied")
    practice.include(tuple(RootFile.existing_in(practice.root)))
    (root, _) = practice.path, practice.measurement
    # A copy keeps the source's wired bus (model_copy reruns no post-init): build the copy's own.
    ctx = type(source)(root=root, actor=source.actor)
    yield ctx
    assert source.comms.goals.todos.get(todo.id) == original


def test_held_goal_replacement_is_a_preserved_refusal_without_writes(held_team):
    ctx = held_team
    comms = ctx.comms
    todo = comms.goals.todos.get("P1")
    goal = comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(), goal_id=todo.held().generation,
    ))
    for action in (ClearGoalAction(), SetGoalAction(text="Replace the held work")):
        with pytest.raises(GoalRefused) as denied:
            comms.goals.update_goal("worker", action)
        with pytest.raises(Refused) as reply:
            OwnerLine.read(OwnerFailure.refused(denied.value).wire()).reply()
        assert reply.value.code == denied.value.code == Refused.INVALID_PARAMS
        assert str(reply.value) == str(denied.value)
        assert comms.registry.require("worker").goal == goal
        assert comms.goals.todos.get("P1") == todo


def link_pull_request(ctx, pid, number):
    """The order's recorded PR link, as `order pr` writes it."""
    store = ctx.comms.goals.todos
    current = store.get(pid)
    order = replace(current.order, pull_requests=(PullRequestLink(current.repo, number),))
    with store.writing() as connection:
        replace(current, order=order, revision=current.revision + 1).upsert(connection)
    return store.get(pid)


def merge_two_commit_fix(source, binaries, number, merge_style: str = '--no-ff'):
    """A two-commit fix under either Git merge style, with the real head in the forge row.

    Returns (base, head, merge); the first source commit only half-fixes count.txt."""
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
    source.git(checkout, 'merge', '-q', '--no-commit', merge_style, f'fix/{number}')
    source.git(checkout, 'commit', '-qm', f'Merge pull request #{number}')
    merge = source.git(checkout, 'rev-parse', 'HEAD').strip()
    source.git(checkout, 'push', '-q', 'origin', 'main', f'{commits[-1]}:refs/pull/{number}/head')
    from github_fixture import GitHubFixture

    GitHubFixture(binaries).put(source.github,
        MergedListedPullRequest(number=number, branch=f'fix/{number}', head=commits[-1],
                                updated='2026-10-23T12:00:00Z', base=WorkBranches.main,
                                reviewable_times=('2026-10-23T12:00:00Z',),
                                merge=PullRequestCommit(merge), merged='2026-10-23T12:00:00Z'),
        body=f'## fix {number}')
    return base, commits[-1], merge


def test_clean_resend_keeps_three_strikes_and_the_existing_holder(held_team, cited_source):
    ctx = held_team
    original = ctx.comms.goals.todos.get("P1")
    for ordinal in range(3):
        original = ctx.comms.goals.todos.apply(original.id, StrikeTodo(
            original.revision, ctx.incarnation, original.held(),
            f"Review {ordinal + 1} found the original duplicated decision still present in the edited source.",
            evidence=(original.order.checked().owner,),
        ))
    marker = ctx.root / "unfinished"
    marker.touch()
    now = time.time()
    replacement = replace(
        order(cited_source), id=OrderId.mint(),
        check=OrderCheck(ctx.repos[0].checkout, f"test ! -e {shlex.quote(str(marker))}"),
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120),
        run=ApplicationOrderRun("The same holder receives corrected work despite three recorded live strikes"),
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
    main(["send", "worker", str(path), "--clean", "--as", "worker"], ctx)
    saved = ctx.comms.goals.todos.get("P1")
    assert saved.order.id != original.order.id
    assert saved.latest_message.message_id != original.latest_message.message_id
    assert saved.strike_count == original.strike_count == 3
    assert saved.held().owner_identity == original.held().owner_identity
    assert ctx.comms.registry.require("worker").goal.require().id == saved.held().generation
    ThreadCaller("worker").invoke(CommsChannelsTool(), ctx.comms)


def test_second_unplanned_order_is_refused_without_recording(held_team, cited_source):
    second_workflow_goal_id = GoalId.mint()
    ctx = held_team
    original = ctx.comms.goals.todos.get("P1")
    marker = ctx.root / "unfinished"
    marker.touch()
    document = OrderFile(
        replace(order(cited_source), id=OrderId.mint(),
                check=OrderCheck(ctx.repos[0].checkout, f"test ! -e {shlex.quote(str(marker))}")),
        "The copied workflow still contains an unfinished decision that prevents completion",
        "This is stupid because assigning another order hides the already unfinished work",
        unplanned_reason="This isolated workflow verifies refusal rather than advancing the production plan.",
        problems=("P2",),
    )
    colleague = ctx.comms.registry.require(ctx.actor_name)
    ctx.comms.goals.todos.create(
        "P2", ctx.repos[0].github, "Repair the second workflow", creator=colleague, assigner=colleague.incarnation, owner=colleague,
        generation=second_workflow_goal_id, request=original.request.require(),
        review_at=RecordedEventTime(time.time() + 1000), order=document.order,
    )
    path = ctx.root / "second-order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    before = ctx.comms.goals.todos.board()
    goal = ctx.comms.registry.require("worker").goal
    # The busy-teammate decision is the Todo transaction's own: --reassign passes the preflight's
    # owner check, so the refusal comes from the write that would have given worker P2.
    with pytest.raises(TeammateOccupied) as refused:
        main(["send", "worker", str(path), "--reassign",
              "colleague holds P2 only as its creator and must hand it to a teammate"], ctx)
    assert ("worker already holds orders P1 (assigned by colleague); running teammates holding no order:"
            in str(refused.value))
    assert ctx.comms.goals.todos.board() == before
    assert ctx.comms.goals.todos.get("P1") == original
    assert ctx.comms.registry.require("worker").goal == goal


@pytest.mark.parametrize("review_at", (UnrecordedEventTime(), RecordedEventTime(12345)))
def test_native_problem_creation_uses_the_owned_review_measurement(
    held_team, cited_source, review_at,
):
    """The native tool measures an omitted review time; recorded source times survive."""
    ctx = held_team
    worker = ctx.comms.registry.require("worker")
    held = ctx.comms.goals.todos.get("P1")
    pending = replace(order(cited_source), id=OrderId.mint())
    measured = MeasuredReview.read(ctx.comms.goals.todos).review_at(pending)
    creation = CreateProblemGoalAction(
        problem="P2", repo=ctx.repos[0].github, text="Repair native order creation",
        assigner=worker.acting_identity, request=held.request.require(),
        review_at=review_at, order=pending,
    )
    ThreadCaller("worker").invoke(
        CommsTodoCreateTool(owner="worker", order=creation), ctx.comms)
    saved = ctx.comms.goals.todos.get("P2")
    assert saved.review_at == review_at.continued_from(measured.at)
    assert saved.order == pending
    assert saved.held().owner_identity == worker.incarnation


def test_a_teammate_files_its_own_second_row_beside_the_one_it_holds(held_team, cited_source):
    """Every PR needs its board row: comms_todo_create from a holder for itself is never busy."""
    ctx = held_team
    worker = ctx.comms.registry.require("worker")
    held = ctx.comms.goals.todos.get("P1")
    creation = CreateProblemGoalAction(
        problem="P2", repo=ctx.repos[0].github, text="Repair the second workflow",
        assigner=worker.acting_identity, request=held.request.require(),
        review_at=RecordedEventTime(time.time() + 1000),
        order=replace(order(cited_source), id=OrderId.mint()))
    ThreadCaller("worker").invoke(CommsTodoCreateTool(owner="worker", order=creation), ctx.comms)
    assert {todo.id for todo in ctx.comms.goals.todos.held_by(worker.incarnation)} == {"P1", "P2"}

    # Nobody sent this order, so its delivery answers no message and goes to the reviewer named.
    reviewer = ctx.comms.registry.require(ctx.actor_name).incarnation
    ThreadCaller("worker").invoke(CommsOrderDeliverTool(
        problem="P2", pull_request=PullRequestLink(held.repo, 7), head="b" * 40,
        report=DeliveryReport("The second workflow was broken by a stale decision; check the PR head."),
        reviewer=SelectedClaimReviewer(reviewer),
    ), ctx.comms)
    claim, = ctx.comms.goals.todos.get("P2").order.team_claims()
    assert (claim.message.target, claim.message.answers) == (reviewer.name, ())


def test_an_order_waiting_on_install_frees_its_holder_for_the_next_order(
    held_team, cited_source, tmp_path,
):
    """Delivered work waits only on its run; its holder takes the next order."""
    ctx = held_team
    merge_two_commit_fix(cited_source, tmp_path / 'bin', 4)
    current = ctx.comms.goals.todos.get('P1')
    check = OrderCheck(cited_source.checkout, 'cat count.txt', CountCheckResult(0))
    with ctx.comms.goals.todos.writing() as connection:
        replace(current, order=replace(current.order, check=check),
                revision=current.revision + 1).upsert(connection)
    link_pull_request(ctx, 'P1', 4)
    with chdir(cited_source.checkout):
        main(["done", "worker", "P1"], ctx)
    assert not ctx.comms.goals.todos.get("P1").order.occupies_holder()
    marker = ctx.root / "unfinished"
    marker.touch()
    second = replace(order(cited_source), id=OrderId.mint(),
                     check=OrderCheck(ctx.repos[0].checkout,
                                      f"test ! -e {shlex.quote(str(marker))}"))
    document = OrderFile(
        second,
        "The copied workflow still contains an unfinished decision that prevents completion",
        "This is stupid because finished work waiting on its run must not idle its holder",
        unplanned_reason=(
            "This isolated workflow verifies holder release"
            " rather than advancing the production plan."
        ),
    )
    path = ctx.root / "second-order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    before = {todo.id for todo in ctx.comms.goals.todos.board()}
    where = f"{second.owner.file}:{second.owner.line}"
    main(["problem", str(path), "--to", "worker", "--where", where], ctx)
    created = [todo for todo in ctx.comms.goals.todos.board()
               if todo.id not in before]
    assert [todo.holder for todo in created] == ["worker"]


@pytest.mark.parametrize('merge_style', ('--no-ff', '--squash'))
def test_done_checks_the_merge_and_records_its_pr_head(
    held_team, cited_source, tmp_path, merge_style: str,
):
    """Merge and squash share the PR-owned source/merge relation, not an ancestry test."""
    ctx = held_team
    base, head, merge = merge_two_commit_fix(cited_source, tmp_path / 'bin', 4, merge_style)
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
    assert (saved.order.delivery.delivered, saved.order.delivery.commit) == (head, merge)
    assert saved.order.delivery.installation_commits == (merge,)
    assert saved.order.check_base(cited_source.checkout) == base


def test_done_without_a_merged_pull_request_records_nothing(held_team):
    ctx = held_team
    original = ctx.comms.goals.todos.get("P1")
    with pytest.raises(ValueError, match="P1 has no merged pull request"):
        main(["done", "worker", "P1"], ctx)
    assert ctx.comms.goals.todos.get("P1") == original


def test_a_problem_not_held_by_the_teammate_is_refused(held_team):
    ctx = held_team
    original = ctx.comms.goals.todos.get("P1")
    with pytest.raises(ValueError, match="worker holds no order on P2"):
        main(["done", "worker", "P2"], ctx)
    with pytest.raises(ValueError, match="other holds no order on P1"):
        ctx.comms.goals.todos.resolve_order(ctx.incarnation, "other", "P1")
    assert ctx.comms.goals.todos.get("P1") == original
