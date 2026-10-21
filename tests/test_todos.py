"""Todo creation and assignment retain exact owners, revisions and goal references."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
from contextlib import chdir, nullcontext
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from multiprocessing import Manager
from pathlib import Path

import pytest

from order_scope_fixture import FIX_SCOPE

from agent_comms.change_report import FileLine
from agent_comms.commander.order import Commander, PullRequestVerb
from agent_comms.commander.standing import UnownedOrderWatch
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.comms import Comms
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.practice import practice_copy
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import OrderCheck, RequestGoalOrder
from agent_comms.launch_root import LaunchRoot
from agent_comms.child_process import ProcessIdentity
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.storage_rules import StorageRule
from agent_comms.store_files import store_lock_path
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.repository_name import RepositoryName
from agent_comms.registration import Registration
from agent_comms.threads import Thread
from agent_comms.todos import (
    Assignment,
    ChangeTodoState,
    DoneTodoState,
    GoalRef,
    ClosedListedPullRequest,
    MergedListedPullRequest,
    PullRequestCommit,
    PendingProblemOrder,
    PullRequestLink,
    PullRequests,
    UnmergedListedPullRequest,
    DeliverTodo,
    OperationDelivery,
    ReleaseTodo,
    StrikeReason,
    Todo,
    TodoConflict,
    TodoError,
    TodoState,
    TodoStore,
    WithdrawTodo,
)
from agent_comms.order_runs import ApplicationOrderRun, MechanicalOrderRun, OrderRun
from agent_comms.work_branches import WorkBranches



def thread(name: str, root: Path, created: float) -> Thread:
    thread_registration = Registration(Path(root) / "registry.json")
    return thread_registration.new_thread(name, frozenset(), str(root), created_at=created)


def order(root: Path):
    return PendingProblemOrder(id="checked-order", owner=FileLine(__file__, 1), scope=FIX_SCOPE, run=MechanicalOrderRun(),
        check=OrderCheck(str(root), "true"), edits=("Fix the problem",),
        banned=("No extra store",), report="Report the delivered commit",
        at=RecordedEventTime(10.0), first_call_deadline=RecordedEventTime(130.0))


def _assign_process(root: str, name: str, created: float, gate) -> tuple[str, str]:
    store = TodoStore(Path(root))
    gate.wait(timeout=5)
    try:
        row = store.assign(
            "todo-1", expected_revision=1,
            owner=thread(name, Path(root), created), assigner=thread("lead", Path(root), 1.0).incarnation,
            generation=f"generation-{name}", goal=GoalRef("lead", 1.0, "goal-1"),
            review_at=RecordedEventTime(1000.0), order=order(Path(root)),
        )
        return "won", row.assignment.owner
    except TodoConflict as error:
        return "conflict", error.current.assignment.owner


def test_create_is_idempotent_and_goal_reference_is_historical(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    goal = GoalRef("lead", 1.0, "goal-1")
    original = store.create(
        "todo-1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "Fix the sidebar", creator=lead, goal=goal,
        owner=lead, generation="initial", review_at=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    assert original.repo == RepositoryName.from_wire("OPENHCSDEV/agent-comms")
    assert original.revision == 1
    assert original.assignment == Assignment("lead", 1.0, "lead", 1.0, "initial")
    assert (
        store.create(
            "todo-1", RepositoryName.from_wire("openhcsdev/agent-comms"), "Fix the sidebar", creator=lead, goal=goal,
            owner=lead, generation="initial", review_at=RecordedEventTime(1000.0), order=order(tmp_path),
        )
        == original
    )
    with pytest.raises(TodoConflict):
        store.create(
            "todo-1", RepositoryName.from_wire("openhcsdev/agent-comms"), "A different task", creator=lead, goal=goal,
            owner=lead, generation="initial", review_at=RecordedEventTime(1000.0), order=order(tmp_path),
        )
    assert TodoStore(tmp_path).list(RepositoryName.from_wire("openhcsdev/agent-comms")) == (original,)
    newer = thread("lead", tmp_path, 2.0)
    assert store.get(original.id).goal == goal
    with pytest.raises(TodoConflict):
        store.apply("todo-1", ChangeTodoState(1, DoneTodoState, newer.incarnation, None))
    assert store.get("todo-1") == original


def test_withdraw_retires_a_released_problem(tmp_path):
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    worker = thread("worker", tmp_path, 2.0)
    todo = store.create("P1", RepositoryName.from_wire("org/repo"), "Obsolete problem",
        creator=lead, owner=worker, generation="first", goal=GoalRef("lead", 1.0, "request"),
        review_at=RecordedEventTime(1000), order=order(tmp_path))
    released = store.apply(todo.id, ReleaseTodo(todo.revision, lead.incarnation, todo.held(),
        StrikeReason("The previous worker stopped before completing this assigned repair on main.")))
    previous = released.holder_state.previous_assignment()
    assert previous == todo.held()
    why = "The requested repair already merged into main through another problem."
    with pytest.raises(TodoConflict):
        store.apply(todo.id, WithdrawTodo(released.revision, lead.incarnation,
            replace(previous, generation="wrong"), why))
    withdrawn = store.apply(todo.id, WithdrawTodo(released.revision, lead.incarnation,
        previous, why))
    assert store.for_commander(lead.incarnation) == ()
    assert withdrawn.state is DoneTodoState and not withdrawn.pending
    assert withdrawn.order.previous == todo.held()
    assert withdrawn.released == released.released and withdrawn.strikes == ()
    assert "withdrawn from worker, no strike" in withdrawn.order.withdrawals(withdrawn)[0]
    assert FieldCodec.decode(Todo, FieldCodec.encode(withdrawn)) == withdrawn


@pytest.mark.parametrize("released", (False, True))
def test_withdraw_refuses_a_delivered_problem(tmp_path, released):
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    todo = store.create("P1", RepositoryName.from_wire("org/repo"), "Delivered problem",
        creator=lead, owner=lead, generation="first", goal=GoalRef("lead", 1.0, "request"),
        review_at=RecordedEventTime(1000), order=order(tmp_path))
    delivered = store.apply(todo.id, DeliverTodo(todo.revision, lead.incarnation,
        OperationDelivery(at=RecordedEventTime(20)), todo.order.id))
    if released:
        delivered = store.apply(todo.id, ReleaseTodo(delivered.revision, lead.incarnation,
            todo.held(), StrikeReason("The worker finished its delivery and no longer needs this assignment.")))
    with pytest.raises(ValueError):
        store.apply(todo.id, WithdrawTodo(delivered.revision, lead.incarnation,
            delivered.holder_state.previous_assignment(), "This delivery must remain on record."))
    assert store.get(todo.id) == delivered


def test_two_processes_assign_one_todo_before_any_worker_is_started(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    store.create(
        "todo-1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "One implementation", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
        owner=lead, generation="initial", review_at=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    with Manager() as manager, ProcessPoolExecutor(max_workers=2) as pool:
        gate = manager.Event()
        results = [
            pool.submit(_assign_process, str(tmp_path), "worker-a", 2.0, gate),
            pool.submit(_assign_process, str(tmp_path), "worker-b", 3.0, gate),
        ]
        gate.set()
        observed = [future.result(timeout=10) for future in results]
    assert sorted(result[0] for result in observed) == ["conflict", "won"]
    assert observed[0][1] == observed[1][1]
    persisted = TodoStore(tmp_path).get("todo-1")
    assert persisted.assignment.owner == observed[0][1]
    assert persisted.revision == 2
    assert len(store.list(RepositoryName.from_wire("OpenHCSDev/agent-comms"))) == 1


def test_validation_rejects_ambiguous_identity_and_bad_state(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    for index, repo in enumerate(("org/repo/branch", "../repo", "org/", "org/repo name")):
        with pytest.raises(ValueError):
            store.create(
                f"invalid-{index}", RepositoryName.from_wire(repo), "task", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
                owner=lead, generation="initial", review_at=RecordedEventTime(1000.0), order=order(tmp_path),
            )
    with pytest.raises(TodoError):
        GoalRef("lead", float("nan"), "goal-1")
    store.create(
        "todo-1", RepositoryName.from_wire("org/repo"), "task", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
        owner=lead, generation="initial", review_at=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    with pytest.raises(ValueError):
        FieldCodec.decode(type[TodoState], "unverified")


def test_merged_order_keeps_its_checked_fields(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    todo = store.create(
        "todo-1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "Fix the sidebar", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
        owner=lead, generation="initial", review_at=RecordedEventTime(1000.0), order=order(tmp_path))
    merged = replace(todo, order=todo.order.checked().merged("2b9c1f1ab"))
    cells = merged.order.board_cells(merged)
    assert cells.startswith(f"{__file__}:1 | lead | checked-order | {RecordedEventTime(1000.0).clock()} | ")
    assert json.loads(cells.split(" | ", 4)[-1]) == FieldCodec.encode(merged.order.checked().check)
    assert merged.order.checked().copy_as(PendingProblemOrder) == todo.order
    assert merged.order.line == "Merged and installed"
    assert merged.order.waiting_on_install(merged, {}) == ()


@pytest.fixture
def pr_commander(tmp_path, monkeypatch):
    """Real Git, CLI command and bus; only the external GitHub answers are synthetic."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    checkout = tmp_path / "lead"
    checkout.mkdir()
    source = Repo(checkout=checkout, github=RepositoryName.from_wire("fixture/orders"))
    source.git(checkout, "init", "-q", "-b", WorkBranches.main)
    source.git(checkout, "-c", "user.name=Order fixture", "-c", "user.email=orders@example.invalid",
               "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "Local merge")
    source.git(checkout, "remote", "add", "origin", f"https://github.com/{source.github.to_wire()}.git")
    merged = source.git(checkout, "rev-parse", "HEAD").strip()
    source.git(checkout, "update-ref", f"refs/remotes/origin/{WorkBranches.main}", merged)
    common = dict(branch="feature/orders", head=merged, updated="2026-10-20T12:00:00Z",
                  base=WorkBranches.main)
    requests = (
        (MergedListedPullRequest(number=1, merge=PullRequestCommit(merged),
                                 merged="2026-10-20T12:00:00Z", **common), "No order metadata."),
        (MergedListedPullRequest(number=2, merge=PullRequestCommit(merged),
                                 merged="2026-10-20T12:00:00Z", **common), "## P343\n\n## P344"),
        (ClosedListedPullRequest(number=3, **common), "Closed without merging."),
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "gh").symlink_to(Path(__file__).with_name("github_cli_stub.py"))
    (binaries / "pull_requests.json").write_text(json.dumps({
        "repo": source.github.to_wire(),
        "requests": {str(row.number): {"body": body, "listed": FieldCodec.encode(row)}
                     for row, body in requests},
    }))
    monkeypatch.setenv("PATH", os.pathsep.join((str(binaries), os.environ["PATH"])))
    lead = Registration(tmp_path / "registry.json").new_thread(
        source.checkout.name, frozenset(), str(source.checkout), created_at=1.0)
    rule = StorageRule(checkout_base=source.checkout.parent)
    (tmp_path / "storage.json").write_text(json.dumps(FieldCodec.encode(rule)))
    Registration(tmp_path / "registry.json").register(lead)
    return Commander(commander=lead.name, root=tmp_path, state=tmp_path / "state",
                     repos=(source,), worktrees=source.checkout.parent,
                     scratch=tmp_path, disk=tmp_path)


def pr_order(ctx, pid, run: OrderRun = MechanicalOrderRun()):
    source = ctx.repos[0]
    lead = ctx.comms.registry.require(ctx.commander)
    return ctx.comms.goals.todos.create(
        pid, source.github, "Fix the sidebar", creator=lead, owner=lead,
        generation="pr-work", goal=GoalRef(lead.name, 1.0, "pr-work"),
        review_at=RecordedEventTime(1000),
        order=replace(order(source.checkout), id=f"order-{pid}", run=run))


def adopt_pr(ctx, pid, number):
    parser = argparse.ArgumentParser()
    PullRequestVerb.add_arguments(parser)
    command = PullRequestVerb.from_arguments(
        parser.parse_args([pid, "--existing", str(number)]))
    with chdir(ctx.repos[0].checkout):
        command.apply(ctx)
    return ctx.comms.goals.todos.get(pid)


def github_body(ctx, number):
    return subprocess.run(
        ("gh", "pr", "view", str(number), "-R", ctx.repos[0].github.to_wire(),
         "--json", "body", "--jq", ".body"),
        capture_output=True, text=True, timeout=60, check=True).stdout


@pytest.mark.parametrize("number,problems,body_identities", (
    pytest.param(1, ("P1",), (), id="without-body-identity"),
    pytest.param(2, ("P343", "P344"), ("P343", "P344"), id="shared-request"),
))
def test_recorded_merged_request_closes_all_linked_orders(pr_commander, number, problems, body_identities):
    ctx = pr_commander
    assert tuple(re.findall(r"\bP[1-9][0-9]*\b|Agent-Comms-Order:", github_body(ctx, number))) == body_identities
    originals = tuple(pr_order(ctx, pid) for pid in problems)
    linked = tuple(adopt_pr(ctx, todo.id, number) for todo in originals)
    link = PullRequestLink(ctx.repos[0].github, number)
    for original, saved in zip(originals, linked):
        assert saved.order.checked().pull_requests == (link,)
        assert saved.revision == original.revision + 1
        assert adopt_pr(ctx, original.id, number) == saved
    requests = PullRequests(linked).listed(ctx.repos[0].github, MergedListedPullRequest, linked[0])
    assert len(requests) == 1
    assert requests[0].problems == tuple(todo.id for todo in linked)
    assert requests[0].orders == tuple(todo.order.id for todo in linked)
    store = TodoStore(ctx.root)
    assert tuple(store.get(todo.id) for todo in originals) == linked
    store.record_merges(store.merged_orders(ctx.incarnation, ctx.comms.registry))
    done = tuple(store.get(todo.id) for todo in originals)
    assert all(todo.state is DoneTodoState for todo in done)
    assert len({todo.order.sha for todo in done}) == 1
    for todo in done:
        assert todo.order.checked().pull_requests == (link,)
        assert ctx.repos[0].contains(todo.order.sha)


def test_unlinked_pr_with_matching_problem_heading_closes_nothing(pr_commander):
    ctx = pr_commander
    assert "## P343" in github_body(ctx, 2)
    original = pr_order(ctx, "P343")
    assert PullRequests((original,)).listed(original.repo, MergedListedPullRequest, original) == ()
    store = TodoStore(ctx.root)
    store.record_merges(store.merged_orders(ctx.incarnation, ctx.comms.registry))
    assert store.get(original.id) == original
    assert original.order.checked().pull_requests == ()


def test_recorded_closed_request_is_not_open_or_merged(pr_commander):
    ctx = pr_commander
    original = pr_order(ctx, "P1")
    linked = adopt_pr(ctx, original.id, 3)
    pulls = PullRequests((linked,))
    assert pulls.listed(linked.repo, UnmergedListedPullRequest, linked) == ()
    assert pulls.listed(linked.repo, MergedListedPullRequest, linked) == ()
    assert len(pulls.listed(linked.repo, ClosedListedPullRequest, linked)) == 1
    store = TodoStore(ctx.root)
    store.record_merges(store.merged_orders(ctx.incarnation, ctx.comms.registry))
    assert store.get(original.id) == linked


def test_watcher_git_and_github_reads_leave_wire_free(pr_commander, monkeypatch):
    """Both real watcher entrypoints, a copied bus, real Git and the external forge stub."""
    print("WATCHPASS SOURCE:", CommanderHeartbeat.watch_pass.__code__.co_filename)
    ctx = pr_commander
    source = ctx.repos[0]
    with LaunchRoot.from_path(ctx.root).selected():
        c = ctx.comms
        c.messaging.initialize_private_initial_protocol()
        ctx.write(ctx.root / "config-home")
        c.goals.update_goal(ctx.commander, SetGoalAction(
            text="Keep the commander request active for both watcher passes",
            order=RequestGoalOrder(), goal_id="pr-work"))
        lead = c.registry.require(ctx.commander)
        c.registry.register(replace(lead, process_identity=ProcessIdentity.capture(os.getpid())))
        originals = tuple(
            adopt_pr(ctx, pr_order(ctx, f"P{index}", run).id, 1)
            for index, run in enumerate((MechanicalOrderRun(), ApplicationOrderRun(
                "Read the copied board and confirm the application order remains assigned")), 1))
        released = pr_order(ctx, "P3")
        c.goals.todos.apply(released.id, ReleaseTodo(
            released.revision, CoreWatcherIncarnation.watcher(), released.held(),
            StrikeReason("The worker stopped before this order was completed and published.")))
        checkout = c.registry.storage_rule.order_checkout(lead.name, released.id)
        source.git(source.checkout, "worktree", "add", "-q", "-b", "released-work", str(checkout))
        merged = source.git(source.checkout, "rev-parse", "HEAD").strip()
        copied_path = ctx.root.parent / f"{ctx.root.name}-watchers"
        with practice_copy(ctx.root, copied_path) as (root, _measurement):
            copied = Comms(root)
            with LaunchRoot.from_path(root).selected():
                # Create the declared inode before the external probes open it read-only.
                with copied.bus.locked():
                    pass
                lock = shlex.quote(str(store_lock_path(copied.bus.path)))
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
                for watch in (CommanderHeartbeat(), UnownedOrderWatch()):
                    start = calls.stat().st_size
                    watch.watch_pass(lambda: nullcontext(copied))
                    readings = calls.read_text()[start:].splitlines()
                    assert readings, type(watch).__name__
                    print(type(watch).__name__, *readings, sep="\n")
                    observed.extend(readings)
                mechanical, application = (copied.goals.todos.get(todo.id) for todo in originals)
                assert mechanical.state is DoneTodoState
                assert mechanical.order.sha == merged
                assert application.order.sha == merged and not application.order.accepted
                assert copied.goals.todos.get(released.id).holder is None
                assert any(line.startswith("gh ") for line in observed)
                assert any(line.startswith("git ") for line in observed)
                held = [line for line in observed if line.split()[1] != "free"]
                assert not held, "Watcher children ran under the wire lock:\n" + "\n".join(held)
        assert tuple(c.goals.todos.get(todo.id) for todo in originals) == originals


def test_installed_merge_closes_only_the_run_that_has_been_proven(pr_commander):
    """Local forge fixture, copied SQLite stores, board and real commander close entrypoint."""
    ctx = pr_commander
    source = ctx.repos[0]
    lead = ctx.comms.registry.require(ctx.commander)
    worker = thread("merge-worker", ctx.root, 2.0)
    ctx.comms.registry.register(worker)
    planned = "Read the copied board and confirm its application order remains assigned after merge"
    link = PullRequestLink(source.github, 1)  # the fixture's locally contained merge
    originals = tuple(
        ctx.comms.goals.todos.create(
            f"P{index}", source.github, "Keep application work held until its run is recorded",
            creator=lead, owner=worker, generation="merge-run",
            goal=GoalRef(lead.name, lead.created_at, "merge-run"),
            review_at=RecordedEventTime(1000),
            order=replace(order(source.checkout), id=f"merge-order-{index}", run=run,
                          pull_requests=(link,)))
        for index, run in enumerate((MechanicalOrderRun(), ApplicationOrderRun(planned)), 1))
    copied_path = ctx.root.parent / f"{ctx.root.name}-copied"
    with practice_copy(ctx.root, copied_path) as (copied, measurement):
        checked = ctx.model_copy(update={"root": copied, "state": copied / "state"})
        store = checked.comms.goals.todos
        store.record_merges(store.merged_orders(checked.incarnation, checked.comms.registry))
        mechanical, application = tuple(store.get(todo.id) for todo in originals)
        assert mechanical.state is DoneTodoState
        assert mechanical.assignment is None
        assert source.contains(mechanical.order.sha)
        assert application.state is originals[1].state
        assert application.assignment == originals[1].assignment
        assert application.holder == worker.name
        assert application.order.sha == mechanical.order.sha
        assert not application.order.accepted
        assert application.order.line == application.order.checked().run.merged_line
        board = checked.board_text()
        assert application.order.line in board
        assert worker.name in board
        print("APPLICATION BOARD:", next(line for line in board.splitlines()
                                          if line.startswith(f"| {application.id}:")))
        store.record_merges(store.merged_orders(checked.incarnation, checked.comms.registry))
        assert store.get(application.id) == application  # no heartbeat revision churn
        assert FieldCodec.decode(Todo, FieldCodec.encode(application)) == application
        with pytest.raises(ToolRefusedError) as refused:
            store.apply(application.id, ChangeTodoState(
                application.revision, DoneTodoState, checked.incarnation, "merge-run"))
        print("CLOSE REFUSAL:", refused.value)
        artifact = copied / "run.txt"
        artifact.write_text(board)
        with pytest.raises(ToolRefusedError):
            checked.close(application.id,
                          "The source grep passed and the merge commit exists in main.",
                          str(artifact))
        assert store.get(application.id) == application
        works = f"{planned}; the copied store and board both retained {worker.name}."
        checked.close(application.id, works, str(artifact))
        installed = store.get(application.id)
        assert installed.state is DoneTodoState
        assert installed.assignment is None
        assert installed.order.accepted
        assert installed.order.works == works
        assert installed.order.artifact == str(artifact)
        assert installed.order.installed(works, str(artifact)) is installed.order
        assert FieldCodec.decode(Todo, FieldCodec.encode(installed)) == installed
        assert tuple(ctx.comms.goals.todos.get(todo.id) for todo in originals) == originals


@pytest.mark.parametrize("number", (0, -1))
def test_existing_selection_leaves_number_validation_to_the_link(pr_commander, number):
    ctx = pr_commander
    original = pr_order(ctx, "P1")
    with pytest.raises(ValueError, match="pull request number must be positive"):
        adopt_pr(ctx, original.id, number)
    assert TodoStore(ctx.root).get(original.id) == original
