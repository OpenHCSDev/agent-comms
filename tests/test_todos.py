"""Todo creation and assignment retain exact owners, revisions and request links."""

from __future__ import annotations


from agent_comms.bus import Bus

import argparse
import json
import os
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import chdir, nullcontext
from dataclasses import dataclass, replace
from multiprocessing import Manager
from pathlib import Path

import pytest

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.commander.order import Commander, PullRequestVerb, SayVerb
from agent_comms.standing_rules import OwedAnswer, StandingRule
from agent_comms.ui_model.unit_state import AwaitingCommander
from agent_comms.commander.standing import RetainedHolderWheelSpin, UnownedOrderWatch, UnownedWheelSpin
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, GoalId, OrderId
from agent_comms.goal_actions import SetGoalAction
from agent_comms.held_orders import HeldOrders
from agent_comms.goals import OrderCheck
from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile
from agent_comms.installer.runtime_resolution import CorePackage
from agent_comms.owner_requests import NewestOpenRequest
from agent_comms.plan_phases import PlanPhase
from agent_comms.launch_root import LaunchRoot
from agent_comms.order_runs import ApplicationOrderRun, MechanicalOrderRun, OrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registration import Registration
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageRule
from agent_comms.store_files import store_lock_path
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.turn_item import NativeTurnId
from agent_comms.threads import Thread
from agent_comms.errors import TodoConflict, TodoError
from agent_comms.todos import (
    Assignment,
    ChangeTodoState,
    ClaimOrder,
    ClosedListedPullRequest,
    OrderClaim,
    OrderMessage,
    DeliverTodo,
    DoneTodoState,
    DraftListedPullRequest,
    MergedListedPullRequest,
    MergedProgress,
    NotOrderCommander,
    OperationDelivery,
    PendingProblemOrder,
    ProgressRead,
    PullRequestCommit,
    PullRequestLink,
    PullRequestSource,
    PullRequests,
    RecordPullRequest,
    ReleaseTodo,
    StrikeReason,
    Todo,
    TodoCommand,
    TodoState,
    TodoStore,
    UnmergedListedPullRequest,
    WithdrawTodo,
)
from agent_comms.work_branches import WorkBranches
from source_revision_fixture import SourceRepository

pytest_plugins = ("test_holder_standing_rules", "test_unowned_checkout_work", "test_proof_build", "github_fixture")


def thread(name: str, root: Path, created: float) -> Thread:
    thread_registration = Registration(Path(root) / "registry.json")
    return Thread(name, frozenset(), str(root), created_at=created)


def order(source: SourceRepository):
    scope = source.scope()
    return PendingProblemOrder(id=OrderId("a" * 12), owner=scope.shape.parents[0], scope=scope, run=MechanicalOrderRun(),
        check=OrderCheck(source.checkout, "true"), edits=("Fix the problem",),
        banned=("No extra store",), report="Report the delivered commit",
        at=RecordedEventTime(10.0), first_call_deadline=RecordedEventTime(130.0))


def test_order_factory_preserves_the_callers_directory_without_creating_source(source_repository):
    before = tuple(source_repository.checkout.iterdir())
    planned = order(source_repository)
    assert planned.check.directory == source_repository.checkout
    assert tuple(source_repository.checkout.iterdir()) == before


def _assign_process(root: str, checkout: str, name: str, created: float, gate) -> tuple[str, str]:
    assignment_goal_id = GoalId.mint()
    store = TodoStore(Path(root))
    gate.wait(timeout=5)
    try:
        row = store.assign(
            "P1", expected_revision=1,
            owner=thread(name, Path(root), created), assigner=thread("lead", Path(root), 1.0).incarnation,
            generation=assignment_goal_id, request=store.get("P1").request.require(),
            review_at=RecordedEventTime(1000.0), order=order(SourceRepository.of(Path(checkout))),
        )
        return "won", row.assignment.owner
    except TodoConflict as error:
        return "conflict", error.current.assignment.owner


def test_create_is_idempotent_and_request_link_is_historical(tmp_path: Path, source_repository) -> None:
    initial_goal_id = GoalId.mint()
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    request = store.add_request(lead.incarnation, "Repair the sidebar").id
    original = store.create(
        "P1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "Fix the sidebar", creator=lead, assigner=lead.incarnation, request=request,
        owner=lead, generation=initial_goal_id, review_at=RecordedEventTime(1000.0),
            order=order(source_repository),
    )
    assert original.repo == RepositoryName.from_wire("OPENHCSDEV/agent-comms")
    assert original.revision == 1
    assert original.assignment == Assignment("lead", 1.0, "lead", 1.0, initial_goal_id)
    assert (
        store.create(
            "P1", RepositoryName.from_wire("openhcsdev/agent-comms"), "Fix the sidebar", creator=lead, assigner=lead.incarnation, request=request,
            owner=lead, generation=initial_goal_id, review_at=RecordedEventTime(1000.0),
                order=order(source_repository),
        )
        == original
    )
    with pytest.raises(TodoConflict):
        store.create(
            "P1", RepositoryName.from_wire("openhcsdev/agent-comms"), "A different task", creator=lead, assigner=lead.incarnation, request=request,
            owner=lead, generation=initial_goal_id, review_at=RecordedEventTime(1000.0),
                order=order(source_repository),
        )
    assert TodoStore(tmp_path).list(RepositoryName.from_wire("openhcsdev/agent-comms")) == (original,)
    newer = thread("lead", tmp_path, 2.0)
    assert store.get(original.id).request == request
    with pytest.raises(NotOrderCommander):
        store.apply("P1", ChangeTodoState(1, DoneTodoState, newer.incarnation, None))
    assert store.get("P1") == original


def test_new_mutation_inherits_revision_admission(tmp_path, source_repository):
    first_goal_id = GoalId.mint()
    mutations = []

    @dataclass(frozen=True)
    class RetitleTodo(TodoCommand, declared_name=None):
        def changed(self, current: Todo) -> Todo:
            mutations.append(current.revision)
            return replace(current, text="Changed through the inherited admission",
                           revision=current.revision + 1)

    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    request = store.add_request(lead.incarnation, "Repair the inherited revision admission")
    original = store.create(
        "P1", RepositoryName.from_wire("org/repo"), "Original title",
        creator=lead, assigner=lead.incarnation, owner=lead, generation=first_goal_id,
        request=request.id, review_at=RecordedEventTime(1000),
        order=order(source_repository),
    )

    changed = store.apply(original.id, RetitleTodo(original.revision))
    assert changed.text == "Changed through the inherited admission"
    with pytest.raises(TodoConflict):
        store.apply(original.id, RetitleTodo(original.revision))
    assert mutations == [original.revision]
    assert store.get(original.id) == changed


def test_withdraw_retires_a_released_problem(tmp_path, source_repository):
    first_goal_id = GoalId.mint()
    wrong_goal_id = GoalId.mint()
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    worker = thread("worker", tmp_path, 2.0)
    request = store.add_request(lead.incarnation, "Withdraw obsolete work").id
    todo = store.create("P1", RepositoryName.from_wire("org/repo"), "Obsolete problem",
        creator=lead, assigner=lead.incarnation, owner=worker, generation=first_goal_id,
            request=request,
        review_at=RecordedEventTime(1000), order=order(source_repository))
    released = store.apply(todo.id, ReleaseTodo(todo.revision, lead.incarnation, todo.held(),
        StrikeReason("The previous worker stopped before completing this assigned repair on main.")))
    previous = released.holder_state.previous_assignment()
    assert previous == todo.held()
    why = "The requested repair already merged into main through another problem."
    with pytest.raises(TodoConflict):
        store.apply(todo.id, WithdrawTodo(released.revision, lead.incarnation,
            replace(previous, generation=wrong_goal_id), why))
    withdrawn = store.apply(todo.id, WithdrawTodo(released.revision, lead.incarnation,
        previous, why))
    assert store.for_commander(lead.incarnation) == ()
    assert withdrawn.state is DoneTodoState and not withdrawn.pending
    assert withdrawn.order.previous == todo.held()
    assert withdrawn.history.events(withdrawn.id, ReleaseTodo) == released.history.events(released.id, ReleaseTodo)
    assert withdrawn.strike_count == 0
    assert "withdrawn from worker, no strike" in withdrawn.order.withdrawals(withdrawn)[0]
    assert FieldCodec.decode(Todo, FieldCodec.encode(withdrawn)) == withdrawn


@pytest.mark.parametrize("released", (False, True))
def test_withdraw_refuses_a_delivered_problem(tmp_path, released, source_repository):
    first_goal_id = GoalId.mint()
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    request = store.add_request(lead.incarnation, "Retain delivered work").id
    todo = store.create("P1", RepositoryName.from_wire("org/repo"), "Delivered problem",
        creator=lead, assigner=lead.incarnation, owner=lead, generation=first_goal_id,
            request=request,
        review_at=RecordedEventTime(1000), order=order(source_repository))
    delivered = store.apply(todo.id, DeliverTodo(todo.revision, lead.incarnation,
        OperationDelivery(at=RecordedEventTime(20)), todo.order.id))
    if released:
        delivered = store.apply(todo.id, ReleaseTodo(delivered.revision, lead.incarnation,
            todo.held(), StrikeReason("The worker finished its delivery and no longer needs this assignment.")))
    with pytest.raises(ValueError):
        store.apply(todo.id, WithdrawTodo(delivered.revision, lead.incarnation,
            delivered.holder_state.previous_assignment(), "This delivery must remain on record."))
    assert store.get(todo.id) == delivered


def test_two_processes_assign_one_todo_before_any_worker_is_started(tmp_path: Path, source_repository) -> None:
    initial_goal_id = GoalId.mint()
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    request = store.add_request(lead.incarnation, "Assign one implementation").id
    store.create(
        "P1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "One implementation", creator=lead, assigner=lead.incarnation, request=request,
        owner=lead, generation=initial_goal_id, review_at=RecordedEventTime(1000.0),
            order=order(source_repository),
    )
    with Manager() as manager, ProcessPoolExecutor(max_workers=2) as pool:
        gate = manager.Event()
        results = [
            pool.submit(_assign_process, str(tmp_path), str(source_repository.checkout), "worker-a", 2.0, gate),
            pool.submit(_assign_process, str(tmp_path), str(source_repository.checkout), "worker-b", 3.0, gate),
        ]
        gate.set()
        observed = [future.result(timeout=10) for future in results]
    assert sorted(result[0] for result in observed) == ["conflict", "won"]
    assert observed[0][1] == observed[1][1]
    persisted = TodoStore(tmp_path).get("P1")
    assert persisted.assignment.owner == observed[0][1]
    assert persisted.revision == 2
    assert len(store.list(RepositoryName.from_wire("OpenHCSDev/agent-comms"))) == 1


def test_validation_rejects_ambiguous_identity_and_bad_state(tmp_path: Path, source_repository) -> None:
    initial_goal_id = GoalId.mint()
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    request = store.add_request(lead.incarnation, "Validate the repository and state").id
    for index, repo in enumerate(("org/repo/branch", "../repo", "org/", "org/repo name")):
        with pytest.raises(ValueError):
            store.create(
                f"invalid-{index}", RepositoryName.from_wire(repo), "task", creator=lead, assigner=lead.incarnation, request=request,
                owner=lead, generation=initial_goal_id, review_at=RecordedEventTime(1000.0),
                    order=order(source_repository),
            )
    store.create(
        "P1", RepositoryName.from_wire("org/repo"), "task", creator=lead, assigner=lead.incarnation, request=request,
        owner=lead, generation=initial_goal_id, review_at=RecordedEventTime(1000.0),
            order=order(source_repository),
    )
    with pytest.raises(ValueError):
        FieldCodec.decode(type[TodoState], "unverified")


def test_merged_order_keeps_its_checked_fields(tmp_path: Path, source_repository) -> None:
    initial_goal_id = GoalId.mint()
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    request = store.add_request(lead.incarnation, "Repair the sidebar").id
    todo = store.create(
        "P1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "Fix the sidebar", creator=lead, assigner=lead.incarnation, request=request,
        owner=lead, generation=initial_goal_id, review_at=RecordedEventTime(1000.0),
            order=order(source_repository))
    merged = replace(todo, order=todo.order.checked().merged("2b9c1f1ab"))
    assert merged.order.checked().check == todo.order.checked().check
    assert merged.order.checked().copy_as(PendingProblemOrder) == todo.order
    assert merged.order.waiting_on_install(merged, {}) == (merged,)



def test_merged_order_refuses_a_new_pull_request(
        tmp_path: Path, source_repository, github_cli) -> None:
    """P605: #3085 linked after #3073 merged left a merged order with an open PR."""
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    repo = RepositoryName.from_wire("OpenHCSDev/agent-comms")
    todo = store.create(
        "P1", repo, "Fix the sidebar", creator=lead, assigner=lead.incarnation,
        request=store.add_request(lead.incarnation, "Repair the sidebar").id, owner=lead,
        generation=GoalId.mint(), review_at=RecordedEventTime(1000.0),
        order=order(source_repository))
    merged = replace(todo, order=todo.order.checked().merged("2b9c1f1ab"))
    held = merged.held()
    link = RecordPullRequest(merged.revision, held.owner_identity,
                             PullRequestLink(repo, 3085), held.generation)
    with pytest.raises(TodoError, match="merged as 2b9c1f1ab"):
        link.changed(merged)
    with pytest.raises(TodoError, match="Ask the commander for a send-back"):
        merged.order.open_pull_request(merged, tmp_path, "Title", "Body", store,
                                       held.owner_identity)
    assert github_cli.calls() == ()
    assert link.changed(todo).order.checked().pull_requests == (PullRequestLink(repo, 3085),)

@pytest.fixture
def pr_commander(tmp_path, monkeypatch, proof_build_checkouts, durable_home, github_cli):
    """One cited source, its real bare remote and installed history; only GitHub is synthetic."""
    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    source = graph.repository
    # Borrow only the plan inputs; the proof fixture owns the commit graph.
    current = Path(__file__).resolve().parents[1]
    shutil.copytree(current / 'plans', source.checkout / 'plans')
    module = source.module_file(PlanPhase.__module__)
    (source.checkout / module).parent.mkdir(parents=True)
    shutil.copyfile(current / module, source.checkout / module)
    source.git(source.checkout, 'add', 'plans', 'src')
    source.git(source.checkout, 'commit', '-qm', 'Declare current commander plans')
    source.git(source.checkout, 'push', '-q', 'origin', 'HEAD:main')
    merged = graph.installed
    for number in (1, 2):
        source.git(source.checkout, 'push', 'origin', f'{merged}:refs/pull/{number}/head')
    common = dict(branch="feature/orders", head=merged, updated="2026-10-20T12:00:00Z",
                  base=WorkBranches.main, reviewable_times=("2026-10-20T12:00:00Z",))
    requests = (
        (MergedListedPullRequest(number=1, merge=PullRequestCommit(merged),
                                 merged="2026-10-20T12:00:00Z", **common), "No order metadata."),
        (MergedListedPullRequest(number=2, merge=PullRequestCommit(merged),
                                 merged="2026-10-20T12:00:00Z", **common), "## P343\n\n## P344"),
        (ClosedListedPullRequest(number=3, **common), "Closed without merging."),
    )
    github_cli.configure(source.github)
    for row, body in requests:
        github_cli.put(source.github, row, body)
    Bus.install(Bus.path_in(tmp_path))
    lead = Thread(
        'lead', frozenset(), str(source.checkout), created_at=1.0)
    # The volume is the tmp_path HOME; the machine's floor belongs to the live bus's rule.
    rule = StorageRule(checkout_base=source.checkout.parent, floor_gib=0)
    (tmp_path / "storage.json").write_text(json.dumps(FieldCodec.encode(rule)))
    Registration(tmp_path / "registry.json").register(lead)
    # The source carries the copied plans; a second checkout of its repository would
    # be compared against the source's installed commit, which it never holds.
    Commander.on_root('lead', (source,), tmp_path)
    bus = wire(tmp_path)
    bus.goals.todos.add_request(lead.incarnation, "Repair the copied order workflow")
    return Commander.read_root(tmp_path)


def pr_order(
    ctx, pid, run: OrderRun = MechanicalOrderRun(), *, owner: Thread | None = None,
    goal_id: GoalId | None = None,
):
    pr_work_goal_id = goal_id if goal_id is not None else GoalId.mint()
    source = ctx.repos[0]
    lead = ctx.comms.registry.require(ctx.commander)
    return ctx.comms.goals.todos.create(
        pid, source.github, "Fix the sidebar", creator=lead, assigner=lead.incarnation,
        owner=owner if owner is not None else lead,
        generation=pr_work_goal_id, request=NewestOpenRequest().for_order(ctx.comms.goals.todos,
            lead.incarnation),
        review_at=RecordedEventTime(1000),
        order=replace(order(source), id=OrderId.mint(), run=run))


def adopt_pr(ctx, pid, number):
    parser = argparse.ArgumentParser()
    PullRequestVerb.add_arguments(parser)
    command = PullRequestVerb.from_arguments(
        parser.parse_args([pid, "--existing", str(number)]))
    todo = ctx.comms.goals.todos.get(pid)
    checkout = Path(ctx.comms.registry.require(todo.held().owner).project_path)
    with chdir(checkout):
        command.apply(ctx)
    return ctx.comms.goals.todos.get(pid)


def github_body(ctx, number):
    return subprocess.run(
        ("gh", "pr", "view", str(number), "-R", ctx.repos[0].github.to_wire(),
         "--json", "body", "--jq", ".body"),
        capture_output=True, text=True, timeout=60, check=True).stdout


@pytest.fixture
def released_published_orders(pr_commander, github_cli):
    """Real clean pushed trees and released rows; only the external forge is synthetic."""
    ctx = pr_commander
    c, source = ctx.comms, ctx.repos[0]
    declared = {}
    for name in ('pi-mi', 'unit-installer', 'unit-backend', 'aaa-idle'):
        checkout = ctx.root / name
        source.git(source.checkout, 'worktree', 'add', '-q', '-b', name, str(checkout))
        source.git(checkout, 'commit', '-q', '--allow-empty', '-m', 'Published work')
        source.git(checkout, 'push', '-q', '-u', 'origin', name)
        declared[name] = c.registry.declare(Thread(
            name, frozenset(), str(checkout), execution=ExternalThreadExecution,
            process_identity=ProcessIdentity.capture(os.getpid())))
        c.transcripts.external(name).finish(
            turn_id=NativeTurnId(name), item_id='idle', events=(AssistantTranscript(
                'Ready for work', position='idle', timestamp=time.time()),))
        assert source.git(checkout, 'status', '--porcelain') == ''
        assert source.git(checkout, 'rev-parse', 'HEAD') == source.git(checkout, 'rev-parse', '@{upstream}')
    originals = {}
    for problem, name, request_type in (
        ('P615', 'pi-mi', UnmergedListedPullRequest),
        ('P637', 'unit-installer', UnmergedListedPullRequest),
        ('P667', 'unit-backend', DraftListedPullRequest),
    ):
        owner = declared[name]
        row = pr_order(ctx, problem, owner=owner)
        number = int(problem[1:])
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        github_cli.put(source.github, request_type(
            number=number, branch=name, head=source.git(Path(owner.worktree), 'rev-parse', 'HEAD').strip(),
            updated=now, base='main', reviewable_times=(now,)))
        row = c.goals.todos.apply(row.id, RecordPullRequest(
            row.revision, owner.incarnation, PullRequestLink(source.github, number), row.held().generation))
        originals[problem] = row
        c.goals.todos.apply(row.id, ReleaseTodo(
            row.revision, CoreWatcherIncarnation.watcher(), row.held(),
            StrikeReason('The watcher released this active unit while its published work remains open.')))
    return ctx, originals


def test_released_open_work_restores_its_own_holder_never_swaps(released_published_orders):
    ctx, originals = released_published_orders
    c = ctx.comms
    goals = {row.holder: c.registry.require(row.holder).goal for row in originals.values()}
    with LaunchRoot.from_path(ctx.root).selected():
        rules = UnownedWheelSpin.paired(
            c, ctx.commander, c.goals.todos.reassignable(), HeldOrders.read(c, ctx.commander),
            ProgressRead(tuple(c.goals.todos.get(pid) for pid in originals)))
        assert {rule.todo.id for rule in rules} == set(originals)
        assert all(isinstance(rule, RetainedHolderWheelSpin) for rule in rules)
        assert StandingRule.enforce_all(c, ctx.commander, rules) == {}
        for problem, before in originals.items():
            after = c.goals.todos.get(problem)
            assert after.held() == before.held()
            assert (after.order, after.review_at) == (before.order, before.review_at)
            assert c.registry.require(before.holder).goal == goals[before.holder]
        assert UnownedWheelSpin.violations(c, ctx.commander) == ()


@pytest.mark.parametrize('disposition', ('retired', 'closed'))
def test_only_retired_holder_or_closed_pr_allows_cross_unit_dispatch(
        released_published_orders, github_cli, disposition):
    ctx, originals = released_published_orders
    c, source = ctx.comms, ctx.repos[0]
    if disposition == 'retired':
        c.registry.register(replace(c.registry.require('pi-mi'), process_identity=NoProcess()))
    else:
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        github_cli.put(source.github, ClosedListedPullRequest(
            number=615, branch='pi-mi', head=source.git(Path(c.registry.require('pi-mi').worktree),
                                                      'rev-parse', 'HEAD').strip(),
            updated=now, base='main', reviewable_times=(now,)))
    with LaunchRoot.from_path(ctx.root).selected():
        rules = UnownedWheelSpin.violations(c, ctx.commander)
        by_problem = {rule.todo.id: rule for rule in rules}
        assert by_problem['P615'].unit == 'aaa-idle'
        assert all(isinstance(by_problem[pid], RetainedHolderWheelSpin) for pid in ('P637', 'P667'))
        assert all(c.goals.todos.get(pid).holder is None for pid in originals)


def test_recorded_closed_request_is_not_open_or_merged(pr_commander):
    ctx = pr_commander
    original = pr_order(ctx, "P1")
    linked = adopt_pr(ctx, original.id, 3)
    pulls = ProgressRead((linked,))
    assert pulls.listed(linked.repo, UnmergedListedPullRequest, linked) == ()
    assert pulls.listed(linked.repo, MergedListedPullRequest, linked) == ()
    assert len(pulls.listed(linked.repo, ClosedListedPullRequest, linked)) == 1


def test_order_is_merged_only_when_every_linked_request_merged(pr_commander, github_cli):
    ctx = pr_commander
    source = ctx.repos[0]
    merged = subprocess.run(("git", "-C", str(source.checkout), "rev-parse", "HEAD"),
                            capture_output=True, text=True, check=True).stdout.strip()
    common = dict(branch="feature/orders", head=merged, updated="2099-01-01T00:00:00Z",
                  base=WorkBranches.main, reviewable_times=("2099-01-01T00:00:00Z",))
    for number in (5, 6):
        github_cli.put(source.github, MergedListedPullRequest(
            number=number, merge=PullRequestCommit(merged), merged="2099-01-01T00:00:00Z",
            **common))
    github_cli.put(source.github, UnmergedListedPullRequest(number=7, **common))

    def found(pid, numbers):
        todo = pr_order(ctx, pid)
        for number in numbers:
            todo = adopt_pr(ctx, todo.id, number)
        return tuple(MergedProgress.found(PullRequestSource(todo, ProgressRead((todo,)))))

    assert found("P1", (5, 7)) == ()
    assert found("P2", (5, 6)) == (MergedProgress(merged), MergedProgress(merged))


def test_watcher_git_and_github_reads_leave_wire_free(pr_commander, monkeypatch):
    """Both real watcher entrypoints, a copied bus, real Git and the external forge stub."""
    pr_work_goal_id = GoalId.mint()
    ctx = pr_commander
    source = ctx.repos[0]
    with LaunchRoot.from_path(ctx.root).selected():
        c = ctx.comms
        c.goals.update_goal(ctx.commander, SetGoalAction(
            text="Keep the commander request active for both watcher passes",
            goal_id=pr_work_goal_id))
        lead = c.registry.require(ctx.commander)
        c.registry.register(replace(lead, process_identity=ProcessIdentity.capture(os.getpid())))
        worker_checkout = ctx.root.parent / "worker-checkout"
        source.git(source.checkout, "worktree", "add", "-q", "--detach", str(worker_checkout))
        worker = Thread("worker", frozenset(), str(worker_checkout), created_at=2.0)
        c.registry.register(worker)
        original = adopt_pr(ctx, pr_order(ctx, "P1", ApplicationOrderRun(
            'Read the copied board and confirm the application order remains assigned',
        ), owner=worker, goal_id=pr_work_goal_id).id, 1)
        merged = PullRequests((original,)).listed(
            source.github, MergedListedPullRequest, original,
        )[0].merge
        sent = c.messaging.send(ctx.commander, worker.name, original.order.instructions(original)).message
        original = c.goals.todos.apply(original.id, OrderMessage(
            original.revision, lead.incarnation, sent.message_id, at=RecordedEventTime(time.time())))
        claim = OrderClaim.for_order(
            original, worker.incarnation, 1, merged, "The repair is ready for review",
            sent.reference, store=c.goals.todos,
        )
        claim = replace(claim, message=replace(
            claim.message, timestamp=time.time() - AwaitingCommander.seconds - 1))
        original = ClaimOrder(
            original.revision, worker.incarnation, original.held().generation, claim,
        ).record(c.goals, original.id)
        assert any(rule.message.message_id == claim.message.message_id
                   for rule in OwedAnswer.opened(c, ctx.commander))
        released = pr_order(ctx, "P3", goal_id=pr_work_goal_id)
        c.goals.todos.apply(released.id, ReleaseTodo(
            released.revision, CoreWatcherIncarnation.watcher(), released.held(),
            StrikeReason("The worker stopped before this order was completed and published.")))
        rule = c.registry.storage_rule
        checkout = rule.order_checkout(rule.initial_roots(lead.name), released.id)
        source.git(source.checkout, "worktree", "add", "-q", "-b", "released-work", str(checkout), merged)
        copied_path = ctx.root.parent / f"{ctx.root.name}-watchers"
        practice = PracticeCopy(ctx.root, copied_path)
        practice.include(tuple(RootFile.existing_in(practice.root)))
        (root, _measurement) = practice.path, practice.measurement
        (root / 'storage.json').write_text(json.dumps(FieldCodec.encode(c.registry.storage_rule)))
        copied = wire(root)
        with LaunchRoot.from_path(root).selected():
            # A practice copy retires every process; this test's process holds the copied lead.
            copied.registry.register(replace(
                copied.registry.require(ctx.commander),
                process_identity=ProcessIdentity.capture(os.getpid())))
            with copied.bus.bus.writer_connection():
                pass  # Create the writer lock through its owner before the child probes it.
            lock = shlex.quote(str(store_lock_path(copied.bus.bus.path)))
            calls = ctx.root / "watcher-children.txt"
            calls.write_text("")
            binaries = ctx.root / "watcher-bin"
            binaries.mkdir()
            for name in ("gh", "git"):
                executable = shutil.which(name)
                assert executable
                shim = binaries / name
                shim.write_text(
                    "#!/bin/sh\n"
                    f"exec 9<{lock}\n"
                    "if flock -n 9; then state=free; flock -u 9; else state=held; fi\n"
                    f"printf '{name} %s %s\\n' \"$state\" \"$*\" >> {shlex.quote(str(calls))}\n"
                    "exec 9<&-\n"
                    f"exec {shlex.quote(executable)} \"$@\"\n")
                shim.chmod(0o755)
            monkeypatch.setenv("PATH", os.pathsep.join((str(binaries), os.environ["PATH"])))
            observed = []
            # Run the standalone watcher while the released checkout is still pending.
            for watch in (UnownedOrderWatch(), CommanderHeartbeat()):
                start = calls.stat().st_size
                watch.watch_pass(lambda: nullcontext(copied))
                readings = calls.read_text()[start:].splitlines()
                assert readings, type(watch).__name__
                print(type(watch).__name__, *readings, sep="\n")
                observed.extend(readings)
            application = copied.goals.todos.get(original.id)
            assert application.order.sha == merged and not application.order.accepted
            assert application.order.commander_claims() == ()
            assert not any(rule.message.message_id == claim.message.message_id
                           for rule in OwedAnswer.opened(copied, ctx.commander))
            report = root / "after-merge.txt"
            report.write_text("Merged; installation evidence is still pending")
            SayVerb(units="#comms", body_file=str(report)).apply(Commander.read_root(root))
            assert copied.goals.todos.get(released.id).held() == released.held()
            assert any(line.startswith("gh ") for line in observed)
            assert any(line.startswith("git ") for line in observed)
            held = [line for line in observed if line.split()[1] != "free"]
            assert not held, "Watcher children ran under the wire lock:\n" + "\n".join(held)
        assert c.goals.todos.get(original.id) == original


def test_reused_enforcement_rejects_a_new_idle_goal(checkout_bus):
    from agent_comms.commander.standing import ReassignedWheelSpin
    from agent_comms.goals import GoalChangedError, NoGoal

    with LaunchRoot.from_path(checkout_bus.root).selected():
        watch = UnownedOrderWatch()
        observed = watch.observe(checkout_bus)
        item, = observed
        assert isinstance(item.rule, ReassignedWheelSpin)
        selected = item.rule.dispatch.unit
        assert selected.goal == NoGoal()
        assert FieldCodec.decode(
            type(item.rule.dispatch.action), FieldCodec.encode(item.rule.dispatch.action)
        ) == item.rule.dispatch.action
        changed = checkout_bus.goals.update_goal(selected.name, SetGoalAction(
            text="New independent work after the idle observation"))
        before = checkout_bus.goals.todos.get(item.rule.todo.id)
        rejected = StandingRule.enforce_all(checkout_bus, item.commander.name, (item.rule,))
        assert checkout_bus.registry.require(selected.name).goal == changed
        assert checkout_bus.goals.todos.get(before.id) == before
        assert isinstance(rejected[item.rule], GoalChangedError)
        assert rejected[item.rule].thread.name == selected.name
        assert rejected[item.rule].expected == selected.goal.captured


@pytest.mark.parametrize("number", (0, -1))
def test_existing_selection_leaves_number_validation_to_the_link(pr_commander, number):
    ctx = pr_commander
    original = pr_order(ctx, "P1")
    with pytest.raises(ValueError, match="pull request number must be positive"):
        adopt_pr(ctx, original.id, number)
    assert TodoStore(ctx.root).get(original.id) == original
