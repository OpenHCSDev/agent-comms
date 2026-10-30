"""Commander forks use registered identity and the original Todo assignment."""

from agent_comms.repo import Checkout
import asyncio
import io
from contextlib import nullcontext
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_comms_hooks.watch import WatchClaim, WatchRecord

from agent_comms.change_report import FileLine, NamedRepository
from agent_comms.commander.order import CommanderContext, OrderFile
from agent_comms.commander.standing import Unjudged
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from commander_heartbeat_fixture import ObservedHeartbeat, capture_frame
from agent_comms.ui_model.unit_state import UnitState
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError, TodoConflict, TodoError
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import (
    FieldCodec,
    GoalId,
    OrderId,
    ToolCallId,
)
from agent_comms.goal_actions import OrderGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.held_orders import HeldOrders
from agent_comms.native_transcript import NativeTranscript
from agent_comms.repository_name import RepositoryName
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.storage_rules import StorageRule
from agent_comms.storage_rules import EditWorkplaces
from agent_comms.turn_digests import OrderEditsRead, OrderEditCountRead, TurnsSince
from agent_comms.installer.backup import StoreBackup
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceOperator,
)
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.order_tools import CommsCommanderLoadTool, CommsHudTool, CommsTodoListTool
from agent_comms.problem_number import ProblemNumber
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.slop import DeclarationDerivation, ExistingLine, Slop
from agent_comms.thread_execution import ExternalThreadExecution, NativeThreadExecution
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from agent_comms.thread_text import ForkedParent, SubagentRole
from agent_comms.threads import Thread
from agent_comms.todos import (
    Assignment,
    DraftListedPullRequest,
    ExpectedProblem,
    FixScope,
    FixShape,
    IncomingWork,
    LocatedProblemFinding,
    PendingProblemOrder,
    ProblemAllocation,
    ProgressRead,
    PullRequests,
    PullRequestLink,
    ReleaseTodo,
    StrikeReason,
    SupersededVerdict,
    Todo,
    UnlocatedProblemFinding,
)
from agent_comms.tool_caller import CommanderForkCaller, SubagentCaller, ThreadCaller
from agent_comms.activity import ActivityState
from agent_comms_hooks import read_gate
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.transcript_events import ToolStartTranscript
from agent_comms.turn_item import NativeItemId, NativeTurnId
from agent_comms.typed_table import HeldBusFile
from agent_comms_hooks import forks
from cli_command import commander
from source_revision_fixture import SourceRepository
from agent_comms.thread_execution import CommanderForkExecution


@pytest.fixture
def fork_bus(tmp_path, configured_repository):
    request_goal_id = GoalId.mint()
    retained_goal_id = GoalId.mint()
    source = SourceRepository.create(tmp_path / "source")
    (source.checkout / "declaration.py").write_text("class AssignmentOwner:\n    pass\n")
    source.git(source.checkout, "add", "declaration.py")
    source.git(source.checkout, "commit", "-qm", "Declare shared assignment owner")
    source.git(source.checkout, "push", "origin", "main")
    comms = wire(tmp_path / "saved-bus")
    comms.channels.create_tag("comms")
    # Two order holders never share one worktree, so each unit has its own checkout.
    units = tuple((name, tmp_path / f"{name}-checkout") for name in ("worker", "other", "retained"))
    for name, worktree in units:
        source.git(source.checkout, "worktree", "add", "-q", "-b", f"{name}-work", str(worktree))
    for name, worktree in (("lead", source.checkout), *units):
        comms.registry.declare(
            Thread(
                name,
                frozenset({"comms"}),
                Checkout(worktree),
                model=DeclaredModel("anthropic/claude-opus-4-6"),
                execution=ExternalThreadExecution,
            )
        )
    comms.goals.update_goal(
        "lead", SetGoalAction(text="Repair the commander workflow", goal_id=request_goal_id)
    )
    request = comms.goals.todos.add_request(
        comms.registry.require("lead").incarnation, "Repair the commander workflow"
    )
    configured_repository(comms, source)
    now = time.time()
    order = PendingProblemOrder(
        owner=source.scope().shape.parents[0],
        scope=source.scope(),
        run=MechanicalOrderRun(),
        check=OrderCheck(source.checkout, "printf 0", CountCheckResult(1)),
        edits=("Repair the existing declaration and remove the duplicate decision.",),
        banned=("No second assignment store.",),
        report="Report the working command.",
        at=RecordedEventTime(now),
        first_call_deadline=RecordedEventTime(now + 600),
    )
    lead = comms.registry.require("lead")
    comms.goals.todos.create(
        "P40",
        source.github,
        "Existing parent problem",
        creator=lead,
        owner=comms.registry.require("retained"),
        assigner=lead.incarnation,
        generation=retained_goal_id,
        review_at=RecordedEventTime(now + 600),
        order=replace(order, id=OrderId.mint()),
        request=request.id,
    )
    copied = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied)
    return wire(copied), source, order


def command(comms, *args):
    """Exercise the real parser and state without cold-importing Core for every verb."""
    return commander("--root", str(comms.root), *args, caller=ThreadCaller('lead'))


def process_command(comms, *args):
    """Independent processes must resolve identity and acquire the same Todo write lock."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_comms.commander.cli import main; main()",
            "--root",
            str(comms.root),
            *ThreadCaller('lead').from_flag(),
            *args,
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        capture_output=True,
        text=True,
        timeout=90,
    )


def document(path, order, problems=()):
    path.write_text(
        json.dumps(
            FieldCodec.encode(
                OrderFile(
                    order=order,
                    problems=problems,
                    defect="Two commander forks can assign conflicting work to the same unit.",
                    why_stupid="This is stupid because the original assignment is not checked.",
                    unplanned_reason=(
                        "This failure blocks the commander from assigning independent work safely."
                    ),
                    slop=(
                        Slop(
                            "IDEN-5",
                            "which fork is assigning this unit",
                            FileLine("owner.py", 2),
                            DeclarationDerivation,
                            ExistingLine(FileLine("declaration.py", 1)),
                        ),
                    ),
                )
            )
        )
    )
    return str(path)


def test_registered_fork_sends_parent_problem_with_fork_actor(fork_bus, tmp_path):
    comms, source, order = fork_bus
    store = comms.goals.todos
    parent = comms.registry.require("lead")
    original = store.get("P40")
    store.apply(
        original.id,
        ReleaseTodo(
            original.revision,
            parent.incarnation,
            original.held(),
            StrikeReason(
                "The original worker stopped and the parent has explicitly released its assignment."
            ),
        ),
    )
    body = document(tmp_path / "parent-problem.json", order, ("P40",))
    sent = command(comms, "order", "send", "worker", body, "--fork", "fork-one")
    assert sent.returncode == 0, sent.stderr
    fork = comms.registry.require("fork-one")
    saved = store.get("P40")
    assert saved.creator_identity == parent.incarnation
    assert store.owner_request(saved.request.require()).commander == parent.incarnation
    assert saved.held().parent_identity == fork.incarnation
    assert saved.latest_message.actor == fork.incarnation
    assert fork.parent.named(parent.name)
    assert fork.goal.require().id != parent.goal.require().id
    # Core's reissue acts as the fork that assigned the unit, under the fork's commander.
    reissuer = saved.acting_assigner
    assert reissuer == fork.incarnation and reissuer.commanding_identity == parent.incarnation
    reissuer.require_may_change(saved)
    with store.snapshot() as db:
        reissued(saved, reissuer).require_free(
            db, saved.held().owner_identity, comms.registry.snapshot())
    print(f"Core send actor: {saved.held().parent}; problem commander: {saved.creator}")
    ctx = CommanderContext.read_root(comms.root).model_copy(
        update={"actor": CommanderForkCaller(fork.name)}
    )
    frame = capture_frame(comms, parent.name, ProgressRead())
    states = UnitState.current(comms, parent.name, frame)
    observed = CommanderObservation(parent, frame, (), states)
    asyncio.run(ObservedHeartbeat((observed,)).run(lambda: nullcontext(comms)))
    hud = ctx.call(CommsHudTool(thread=ctx.commander))
    assert hud.commander == parent.incarnation
    assert [(unit.unit, tuple(order.problem for order in unit.orders)) for unit in hud.units] == [
        (saved.held().owner_identity, ("P40",))
    ]
    assert set(ctx.model_copy(deep=True).progress()) == {"P40"}
    assert ctx.held_orders() == (saved,)
    assert ctx.call(CommsTodoListTool(thread=ctx.commander)).todos == (saved,)
    load = ctx.call(CommsCommanderLoadTool(thread=ctx.commander))
    assert load.load.owed == 1 and load.load.timers_up == 0 and load.load.unjudged == 0
    assert "unjudged edits" in str(load.load)
    refused = command(comms, "order", "send", "worker", body, "--fork", "fork-two")
    assert refused.returncode != 0 and "worker is assigned by fork-one" in refused.stderr
    assert store.get("P40") == saved
    interim = tmp_path / "interim.txt"
    interim.write_text("The first review is complete; another check remains.")
    posted = command(comms, "order", "say", "#comms", str(interim), "--fork", "fork-one")
    assert posted.returncode == 0, posted.stderr
    assert comms.registry.status("fork-one").receiving
    assert store.get("P40") == saved
    refused = command(comms, "order", "retire")
    assert refused.returncode != 0 and "Only a commander fork can retire" in refused.stderr
    retired = command(comms, "order", "retire", "--fork", "fork-one")
    assert retired.returncode == 0, retired.stderr
    assert not comms.registry.status("fork-one").receiving
    assert store.get("P40") == saved
    # The ended fork's open order is the commander's to replace; nobody else's.
    with store.snapshot() as db:
        reissued(saved, parent.incarnation).require_free(
            db, saved.held().owner_identity, comms.registry.snapshot())


def reissued(todo, assigner):
    """The incoming work of a resend of ``todo``'s own order by ``assigner``."""
    return IncomingWork(assigner, frozenset({todo.id}), frozenset({todo.order.checked().id}))


def record_edits(comms, source, count):
    """One retained-holder transcript turn with ``count`` original edit calls."""
    actor = comms.registry.require("retained").incarnation
    ExternalTranscript(comms.root, actor).finish(
        turn_id=NativeTurnId("parent-work"),
        item_id=NativeItemId("parent-edit"),
        events=tuple(
            ToolStartTranscript(
                position=position,
                tool_call_id=ToolCallId(f"parent-edit-{position}"),
                tool_name="edit",
                timestamp=time.time(),
                tool=ToolDeclaration.record(
                    "edit",
                    {
                        "path": str(source.checkout / "owner.py"),
                        "edits": [{"oldText": "", "newText": f"class ReviewedOwner{position}: pass"}],
                    },
                    call_id=ToolCallId(f"parent-edit-{position}"),
                ),
            )
            for position in range(count)
        ),
    )


def test_fork_and_parent_judge_show_same_unjudged_edits(fork_bus):
    comms, source, order = fork_bus
    record_edits(comms, source, 1)
    own = command(comms, "judge", "show")
    fork = command(comms, "judge", "show", "--fork", "review-fork")
    for result in (own, fork):
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-1] == "1 unjudged"
        assert "parent-edit-0" in result.stdout
    print("judge show: 1 unjudged; judge show --fork review-fork: 1 unjudged")


def test_unjudged_reminder_names_each_order_with_edits(fork_bus):
    comms, source, _ = fork_bus
    record_edits(comms, source, Unjudged.limit + 1)
    assert Unjudged.reason(comms, "lead") == (
        f"{Unjudged.limit + 1} edits since each order's last head verdict: "
        f"retained P40 {Unjudged.limit + 1}")


def test_edits_under_a_merged_order_are_nobodys_review(fork_bus):
    """Overload, the HUD and the judge read one judgeable owner: a merged head gates nothing."""
    from agent_comms.todos import MergedProgress, NoCommitProgress

    comms, source, _ = fork_bus
    record_edits(comms, source, 3)
    holds = HeldOrders.read(comms, "lead").live
    open_order = {hold.todo.id: NoCommitProgress() for hold in holds}
    merged = {hold.todo.id: MergedProgress("a" * 40) for hold in holds}
    workplaces = EditWorkplaces.capture(
        comms, comms.registry.snapshot(), holds, ProgressRead())
    assert [read.read() for read in OrderEditCountRead.judgeable(
        comms.root, holds, open_order, workplaces)] == [3]
    assert tuple(OrderEditCountRead.judgeable(comms.root, holds, merged, workplaces)) == ()


@pytest.fixture
def edit_sources(fork_bus, tmp_path, configured_repository):
    """Two actual origins, a held Core-like source and its real allocated worktree."""
    comms, source, _ = fork_bus
    foreign = SourceRepository.create(tmp_path / "foreign", RepositoryName("example", "foreign"))
    configured_repository(comms, source, foreign)
    holder = comms.registry.require("retained")
    rule = StorageRule.load(comms.root)
    primary = rule.order_checkout(rule.roots_of(holder), "P40")
    primary.parent.mkdir(parents=True, exist_ok=True)
    source.git(source.checkout, "worktree", "add", "-q", "-b", "held-P40", str(primary))
    return comms, source, foreign, primary


def edit_workplaces(comms):
    snapshot = comms.registry.snapshot()
    holds = HeldOrders.of(comms.goals.todos, snapshot, "lead").live
    history = comms.goals.todos.history_for_commander(snapshot.require("lead").incarnation)
    return holds, EditWorkplaces.capture(comms, snapshot, holds, ProgressRead(history))


def save_edit_order(comms, todo):
    """Publish an actual retained order row, not a preselected workplace answer."""
    with comms.goals.todos.writing() as db:
        todo.upsert(db)


def another_edit_order(comms):
    """A saved multi-order state, supported for records predating one-unit/one-order."""
    original = comms.goals.todos.get("P40")
    lead = comms.registry.require("lead").incarnation
    return Todo.ordered(
        "P41", original.repo, "Independent second source change", lead,
        Assignment.of(comms.registry.require("retained").incarnation, lead, GoalId.mint()),
        RecordedEventTime(time.time() + 600),
        replace(original.order.checked(), id=OrderId.mint(),
                at=RecordedEventTime(time.time() + 10)), original.request.require(),
    )


def captured_edit_sources(comms, paths, session):
    """Original external tool starts and an actual Pi journal, with the same edit paths."""
    actor = comms.registry.require("retained").incarnation
    timestamp = time.time()
    stamp = datetime.fromtimestamp(timestamp, UTC).isoformat()
    calls = tuple((ToolCallId(f"source-edit-{i}"), {
        "path": str(path), "edits": [{"oldText": "", "newText": f"VALUE = {i}"}],
    }) for i, path in enumerate(paths))
    events = tuple(ToolStartTranscript(
        position=i, tool_call_id=identity, tool_name="edit", timestamp=timestamp,
        tool=ToolDeclaration.record("edit", arguments, call_id=identity),
    ) for i, (identity, arguments) in enumerate(calls))
    ExternalTranscript(comms.root, actor).finish(
        turn_id=NativeTurnId("source-work"), item_id=NativeItemId("source-work"), events=events)
    rows = (
        {"type": "session", "version": 3, "id": "source-work", "timestamp": stamp},
        {"type": "message", "id": "source-input", "timestamp": stamp,
         "message": {"role": "user", "content": [{"type": "text", "text": "Edit these sources"}]}},
        {"type": "message", "id": "source-tools", "parentId": "source-input", "timestamp": stamp,
         "message": {"role": "assistant", "content": [
             {"type": "toolCall", "id": identity, "name": "edit", "arguments": arguments}
             for identity, arguments in calls]}},
    )
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return calls


@pytest.mark.parametrize("execution", (NativeThreadExecution, ExternalThreadExecution))
def test_edit_membership_preserves_native_and_external_foreign_records(edit_sources, tmp_path, execution):
    comms, source, foreign, primary = edit_sources
    scratch = StorageRule.load(comms.root).roots_of(comms.registry.require("retained")).scratch
    paths = (primary / "owner.py", foreign.checkout / "owner.py", scratch / "comment.md",
             scratch / "plugin" / "introspection_trace.py", scratch / "typemap" / "probe.py",
             primary / "new_source.py", primary / "deleted_source.py")
    deleted = paths[-1]
    deleted.write_text("BEFORE = True\n")
    deleted.unlink()
    session = tmp_path / "original-native.jsonl"
    calls = captured_edit_sources(comms, paths, session)
    original_bytes = session.read_bytes()
    before = comms.goals.todos.get("P40")
    holds, workplaces = edit_workplaces(comms)
    hold, = holds
    holder = replace(hold.holder, execution=execution,
                     session_file=RegistrySessionLocation.from_path(session, comms.root))
    read = OrderEditsRead(
        comms.root, holder, TurnsSince(before.order.checked().at.recorded_at("An order")),
        before, workplaces[holder.incarnation])
    expected = {calls[index][0] for index in (0, 5, 6)}
    assert {edit.id for edit in read.read()} == expected
    count = OrderEditCountRead(read.root, holder, read.bound, before, read.workplaces)
    assert count.read() == len(expected)
    assert comms.goals.todos.get("P40") == before
    assert before.judged_calls == ()
    assert session.read_bytes() == original_bytes
    native = NativeTranscript(session)
    native_events = tuple(event for turn in native.turns(TurnsSince(0)) for event in turn.events)
    native_ids = {edit.id for event in native_events
                  for edit in event.edit_calls(holder.name, "P40", 0)}
    with ExternalTranscript(comms.root, holder.incarnation).reading() as external:
        external_ids = {edit.id for turn in external.turns(TurnsSince(0)) for event in turn.events
                        for edit in event.edit_calls(holder.name, "P40", 0)}
    assert native_ids == external_ids == {identity for identity, _ in calls}


@pytest.mark.parametrize("declare_companion", (
    pytest.param(lambda order, location: replace(
        order, scope=FixScope(FixShape(order.scope.shape.parents, (location,))),
        finding=UnlocatedProblemFinding()), id="scope-unlocated-finding"),
    pytest.param(lambda order, location: replace(
        order, finding=LocatedProblemFinding(location)), id="located-finding"),
))
def test_canonical_primary_companion_and_released_roots_take_precedence(edit_sources, declare_companion):
    comms, source, foreign, primary = edit_sources
    store = comms.goals.todos
    todo = store.get("P40")
    owner = comms.registry.require("retained")
    rule = StorageRule.load(comms.root)
    roots = rule.roots_of(owner)
    companion = rule.repository_checkout(roots, todo.id, foreign.github)
    foreign.git(foreign.checkout, "worktree", "add", "-q", "-b", "companion-P40", str(companion))
    # A qualified source declaration, not origin equality alone, authorizes the companion.
    location = FileLine("owner.py", 2, NamedRepository(foreign.github))
    save_edit_order(comms, replace(todo, order=declare_companion(todo.order, location)))
    released = another_edit_order(comms)
    save_edit_order(comms, released)
    released_path = rule.order_checkout(roots, released.id)
    source.git(source.checkout, "worktree", "add", "-q", "-b", "released-P41", str(released_path))
    store.apply(released.id, ReleaseTodo(
        released.revision, comms.registry.require("lead").incarnation, released.held(),
        StrikeReason("The independent order is released; its checkout must not spill.")))
    current = store.get("P40")
    save_edit_order(comms, replace(current, order=replace(
        current.order, check=replace(current.order.check, directory=released_path))))
    _, workplaces = edit_workplaces(comms)
    selected = workplaces[owner.incarnation]
    assert selected.problems_for(str(primary / "new.py")) == frozenset(("P40",))
    assert selected.problems_for(str(companion / "new.py")) == frozenset(("P40",))
    assert selected.problems_for(str(released_path / "new.py")) == frozenset()
    # A registered foreign repository in an undeclared companion allocation stays unowned.
    current = store.get("P40")
    save_edit_order(comms, replace(current, order=replace(
        current.order, scope=source.scope(), finding=UnlocatedProblemFinding())))
    _, workplaces = edit_workplaces(comms)
    assert workplaces[owner.incarnation].problems_for(str(companion / "new.py")) == frozenset()


def test_misleading_primary_allocation_refuses_foreign_origin(edit_sources):
    comms, source, foreign, _ = edit_sources
    another = another_edit_order(comms)
    save_edit_order(comms, another)
    holder = comms.registry.require("retained")
    rule = StorageRule.load(comms.root)
    misleading = rule.order_checkout(rule.roots_of(holder), another.id)
    foreign.git(foreign.checkout, "worktree", "add", "-q", "-b", "wrong-origin", str(misleading))
    _, workplaces = edit_workplaces(comms)
    assert workplaces[holder.incarnation].problems_for(str(misleading / "owner.py")) == frozenset()
    # P41's later timestamp never wins the ambiguous shared pre-PR source either.
    assert workplaces[holder.incarnation].problems_for(str(source.checkout / "owner.py")) == frozenset(("P40", "P41"))


def test_noncanonical_worktree_needs_qualified_link_not_matching_origin(edit_sources, tmp_path, github_cli):
    comms, source, _, _ = edit_sources
    holder = comms.registry.require("retained")
    linked = StorageRule.load(comms.root).roots_of(holder).scratch / "legacy-source"
    linked.parent.mkdir(parents=True, exist_ok=True)
    unrelated = tmp_path / "same-origin-no-link"
    source.git(source.checkout, "worktree", "add", "-q", "-b", "linked-repair", str(linked))
    source.git(source.checkout, "worktree", "add", "-q", "-b", "unlinked-work", str(unrelated))
    _, workplaces = edit_workplaces(comms)
    assert workplaces[holder.incarnation].problems_for(str(source.checkout / "new.py")) == frozenset(("P40",))
    assert workplaces[holder.incarnation].problems_for(str(linked / "new.py")) == frozenset()
    todo = comms.goals.todos.get("P40")
    row = DraftListedPullRequest(number=132, branch="linked-repair", head=source.main_commit,
                                 updated="2026-10-28T12:00:00Z", base="main", reviewable_times=())
    github_cli.put(source.github, row, body="P40 source ownership")
    save_edit_order(comms, replace(todo, order=replace(
        todo.order, pull_requests=(PullRequestLink(source.github, row.number),))))
    _, workplaces = edit_workplaces(comms)
    selected = workplaces[holder.incarnation]
    assert selected.problems_for(str(linked / "new.py")) == frozenset(("P40",))
    assert selected.problems_for(str(unrelated / "new.py")) == frozenset()
    assert selected.problems_for(str(source.checkout / "new.py")) == frozenset()
    # The explicit retained source may be a different clone: it will not occur in the
    # registered checkout's worktree list, but its own branch still has to match the PR.
    separate = tmp_path / "explicit-source-clone"
    source.git(tmp_path, "clone", "-q", str(source.remote_path), str(separate))
    source.git(separate, "checkout", "-q", "-b", "linked-repair")
    independent = SourceRepository.of(separate)
    assert independent.github == source.github
    pulls = PullRequests()
    registered_trees = pulls.worktrees(source)
    independent_trees = pulls.worktrees(independent)
    assert str(linked) in registered_trees["linked-repair"]
    assert str(separate) not in registered_trees["linked-repair"]
    assert str(separate) in independent_trees["linked-repair"]
    assert str(linked) not in independent_trees["linked-repair"]
    assert pulls.worktrees(source) is registered_trees
    assert pulls.worktrees(independent) is independent_trees
    alias = SourceRepository.of(separate / ".." / separate.name)
    assert alias.checkout != independent.checkout
    assert pulls.worktrees(alias) is independent_trees
    current = comms.goals.todos.get("P40")
    save_edit_order(comms, replace(current, order=replace(
        current.order, check=replace(current.order.check, directory=separate))))
    _, workplaces = edit_workplaces(comms)
    assert workplaces[holder.incarnation].problems_for(str(separate / "new.py")) == frozenset(("P40",))
    source.git(separate, "checkout", "-q", "-b", "wrong-branch")
    _, workplaces = edit_workplaces(comms)
    assert workplaces[holder.incarnation].problems_for(str(separate / "new.py")) == frozenset()


def test_ambiguous_pre_pr_work_never_picks_latest(edit_sources, tmp_path):
    comms, source, _, primary = edit_sources
    save_edit_order(comms, another_edit_order(comms))
    session = tmp_path / "ambiguous.jsonl"
    calls = captured_edit_sources(comms, (source.checkout / "owner.py", primary / "new.py"), session)
    shown = command(comms, "judge", "show")
    assert shown.returncode == 0, shown.stderr
    assert calls[0][0] not in shown.stdout and calls[1][0] in shown.stdout
    assert shown.stdout.splitlines()[-1] == "1 unjudged"
    assert all(not todo.judged_calls for todo in comms.goals.todos.history_for_commander(
        comms.registry.require("lead").incarnation))


def test_missing_unknown_allocation_still_overrides_an_enclosing_source(edit_sources):
    comms, source, _, _ = edit_sources
    # A genuine source encloses the allocation base; no physical P999 checkout exists.
    # The source cannot swallow a historical/deleted allocation by generic containment.
    rule = replace(StorageRule.load(comms.root), checkout_base=source.checkout)
    StorageRule.path_in(comms.root).write_text(json.dumps(FieldCodec.encode(rule)))
    holder = comms.registry.require("retained")
    missing = rule.order_checkout(rule.roots_of(holder), "P999")
    assert not missing.exists()
    _, workplaces = edit_workplaces(comms)
    selected = workplaces[holder.incarnation]
    assert selected.problems_for(str(source.checkout / "ordinary.py")) == frozenset(("P40",))
    assert selected.problems_for(str(missing / "old.py")) == frozenset()


def test_symlink_aliases_and_foreign_escapes_cannot_mint_membership(edit_sources, tmp_path):
    comms, source, foreign, primary = edit_sources
    alias = tmp_path / "alias"
    alias.symlink_to(primary, target_is_directory=True)
    escape = primary / "foreign.py"
    escape.symlink_to(foreign.checkout / "owner.py")
    holder = comms.registry.require("retained")
    _, workplaces = edit_workplaces(comms)
    selected = workplaces[holder.incarnation]
    assert selected.problems_for(str(alias / "owner.py")) == frozenset()
    assert selected.problems_for(str(escape)) == frozenset()


def test_open_unit_consumers_agree_on_one_assignment(fork_bus):
    comms, source, order = fork_bus
    store = comms.goals.todos
    todo = store.get("P40")
    owner = comms.registry.require("retained").incarnation
    assert store.holds_pending(owner)
    assert store.for_goal(owner, todo.held().generation) == (todo,)
    assert store.held_by(comms.registry.require("lead").incarnation, owner) == (todo,)
    other_birth = RecordedThreadIncarnation(owner.name, owner.wire_birth + 1)
    assert not store.holds_pending(other_birth)
    assert store.for_goal(other_birth, todo.held().generation) == ()
    snapshot = comms.registry.snapshot()
    sibling = reissued(todo, comms.registry.require("other").incarnation)
    with store.snapshot() as db:
        reissued(todo, todo.held().parent_identity).require_free(db, owner, snapshot)
        sibling.require_free(db, other_birth, snapshot)
        queries = []
        db.set_trace_callback(queries.append)
        with pytest.raises(TodoError, match="retained is assigned by lead"):
            sibling.require_free(db, owner, snapshot)
        db.set_trace_callback(None)
        plan = db.execute("EXPLAIN QUERY PLAN " + queries[0]).fetchall()
        print("Shared open-unit query plan:", [tuple(row) for row in plan])


def test_a_sibling_fork_assigns_a_unit_whose_order_waits_on_install(fork_bus):
    """Merged work waits only on its run, so its holder is free for any assigner."""
    comms, source, order = fork_bus
    store = comms.goals.todos
    todo = store.get("P40")
    owner = todo.held().owner_identity
    sibling = reissued(todo, comms.registry.require("other").incarnation)
    with store.snapshot() as db, pytest.raises(TodoError, match="finish or release"):
        sibling.require_free(db, owner, comms.registry.snapshot())
    with store.writing() as db:
        replace(todo, order=todo.order.merged("0" * 40), revision=todo.revision + 1).upsert(db)
    assert not store.get("P40").order.occupies_holder()
    with store.snapshot() as db:
        sibling.require_free(db, owner, comms.registry.snapshot())


def test_a_value_error_refusal_prints_instead_of_a_traceback(fork_bus):
    """The commander CLI fails on the comms CLI's one failure set."""
    comms, _, _ = fork_bus
    refused = command(comms, "order", "done", "retained", "P40")
    assert refused.returncode == 1
    assert refused.stderr.startswith("Refused: P40 has no merged pull request"), refused.stderr


def test_two_commanders_allocate_distinct_global_problem_ids(fork_bus, tmp_path):
    comms, source, order = fork_bus

    def assign(pair):
        name, unit = pair
        body = document(tmp_path / f"{name}.json", replace(order, id=OrderId.mint()))
        return process_command(
            comms, "order", "problem", body, "--to", unit, "--where", "owner.py:2", "--fork", name
        )

    with ThreadPoolExecutor(max_workers=2) as commands:
        results = tuple(commands.map(assign, (("fork-a", "worker"), ("fork-b", "other"))))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    todos = tuple(
        todo
        for todo in comms.goals.todos.held_for_commander(comms.registry.require("lead").incarnation)
        if todo.id != "P40"
    )
    assert {todo.id for todo in todos} == {"P41", "P42"}
    assert {todo.creator for todo in todos} == {"lead"}
    assert {todo.held().parent for todo in todos} == {"fork-a", "fork-b"}
    print("Concurrent fork allocations:", sorted((todo.held().parent, todo.id) for todo in todos))


def test_two_commanders_cannot_assign_the_same_unit_concurrently(fork_bus, tmp_path):
    comms, source, order = fork_bus

    def assign(name):
        body = document(tmp_path / f"{name}.json", replace(order, id=OrderId.mint()))
        return process_command(
            comms,
            "order",
            "problem",
            body,
            "--to",
            "worker",
            "--where",
            "owner.py:2",
            "--fork",
            name,
        )

    with ThreadPoolExecutor(max_workers=2) as commands:
        results = tuple(commands.map(assign, ("fork-a", "fork-b")))
    assert sum(result.returncode == 0 for result in results) == 1, [r.stderr for r in results]
    held = tuple(
        todo
        for todo in comms.goals.todos.held_for_commander(comms.registry.require("lead").incarnation)
        if todo.id != "P40"
    )
    assert len(held) == 1 and held[0].holder == "worker"
    refused = next(result for result in results if result.returncode)
    # One decision inside the Todo transaction: the loser always meets the winner's held order
    # as other work, the same refusal the sequential sends below give.
    assert occupied("worker", held[0]) in refused.stderr
    assert "Traceback" not in refused.stderr, refused.stderr
    print(refused.stderr.strip())


def occupied(unit, todo):
    """The busy-unit refusal for other work, up to its idle-unit hint."""
    return (f"{unit} already holds orders {todo.id} (assigned by {todo.held().parent}); "
            "running units holding no order:")


def open_problem(comms, source, order, pid, owner):
    """A problem the commander ordered to ``owner`` under its own order."""
    lead = comms.registry.require("lead")
    return comms.goals.todos.create(
        pid, source.github, f"Repair {pid}", creator=lead, assigner=lead.incarnation,
        owner=comms.registry.require(owner), generation=GoalId.mint(),
        review_at=RecordedEventTime(time.time() + 600), order=replace(order, id=OrderId.mint()),
        request=comms.goals.todos.get("P40").request.require(),
    )


def released(comms, todo):
    """The problem with no holder, as Core's release leaves it."""
    return comms.goals.todos.apply(todo.id, ReleaseTodo(
        todo.revision, comms.registry.require("lead").incarnation, todo.held(),
        StrikeReason("The fixture frees this problem so one order can name it.")))


def test_sequential_fork_assignments_refuse_with_the_race_message(fork_bus, tmp_path):
    comms, source, order = fork_bus
    first, second = (command(comms, "order", "problem",
                             document(tmp_path / f"{name}.json", replace(order, id=OrderId.mint())),
                             "--to", "worker", "--where", "owner.py:2", "--fork", name)
                     for name in ("fork-a", "fork-b"))
    assert first.returncode == 0, first.stderr
    held, = (todo for todo in comms.goals.todos.held_for_commander(
        comms.registry.require("lead").incarnation) if todo.holder == "worker")
    assert second.returncode != 0 and occupied("worker", held) in second.stderr


@pytest.mark.parametrize("held_first", (True, False))
def test_one_order_takes_a_new_problem_and_one_the_unit_holds_in_either_order(
    fork_bus, tmp_path, held_first,
):
    """A group member the unit still holds under an older order is part of this order,
    not other work, and every member is written by the one send."""
    comms, source, order = fork_bus
    store = comms.goals.todos
    fresh = released(comms, store.get("P40"))
    held = open_problem(comms, source, order, "P41", "worker")
    group = (held.id, fresh.id) if held_first else (fresh.id, held.id)
    sent = replace(order, id=OrderId.mint())
    result = command(comms, "order", "send", "worker",
                     document(tmp_path / "group.json", sent, group))
    assert result.returncode == 0, result.stderr
    for pid in group:
        todo = store.get(pid)
        assert todo.holder == "worker" and todo.order.checked().id == sent.id
    goal = comms.registry.require("worker").goal.require()
    assert {todo.id for todo in store.for_goal(
        comms.registry.require("worker").incarnation, goal.id)} == set(group)


def test_unrelated_held_work_refuses_the_same_assigner(fork_bus, tmp_path):
    comms, source, order = fork_bus
    store = comms.goals.todos
    released(comms, store.get("P40"))
    open_problem(comms, source, order, "P41", "worker")
    fresh, held = store.get("P40"), store.get("P41")
    goal = comms.registry.require("worker").goal
    refused = command(comms, "order", "send", "worker",
                      document(tmp_path / "other.json", replace(order, id=OrderId.mint()),
                               (fresh.id,)))
    assert refused.returncode != 0 and occupied("worker", held) in refused.stderr
    assert (store.get(fresh.id), store.get(held.id)) == (fresh, held)
    assert comms.registry.require("worker").goal == goal


def test_a_sibling_forks_hold_refuses_the_whole_group(fork_bus, tmp_path):
    """The group is one write: a sibling's hold on one member writes none of the others."""
    comms, source, order = fork_bus
    store = comms.goals.todos
    first = released(comms, store.get("P40"))
    sent = command(comms, "order", "send", "worker",
                   document(tmp_path / "first.json", replace(order, id=OrderId.mint()),
                            (first.id,)), "--fork", "fork-one")
    assert sent.returncode == 0, sent.stderr
    released(comms, open_problem(comms, source, order, "P41", "other"))
    held, other = store.get(first.id), store.get("P41")
    refused = command(comms, "order", "send", "worker",
                      document(tmp_path / "group.json", replace(order, id=OrderId.mint()),
                               (other.id, held.id)), "--fork", "fork-two")
    assert refused.returncode != 0
    assert (f"worker is assigned by fork-one on open order {held.order.checked().id} "
            f"({held.id})") in refused.stderr
    assert (store.get(held.id), store.get(other.id)) == (held, other)


def test_a_stale_later_member_rolls_back_the_written_first_member(fork_bus, tmp_path):
    """The first member passes and is written before a later member's stale revision refuses:
    the one Todo transaction and the goal update roll back together, so nothing changes."""
    comms, source, order = fork_bus
    store = comms.goals.todos
    first = released(comms, store.get("P40"))
    stale = released(comms, open_problem(comms, source, order, "P41", "other"))
    lead = comms.registry.require("lead")
    goal = comms.registry.require("worker").goal
    with pytest.raises(TodoConflict):
        comms.goals.update_goal("worker", OrderGoalAction(
            problem=first.id, expected_revision=first.revision,
            together=(ExpectedProblem(stale.id, stale.revision - 1),),
            assigner=lead.incarnation, commander=lead.name,
            request=first.request.require(), review_at=RecordedEventTime(time.time() + 600),
            order=replace(order, id=OrderId.mint())))
    assert (store.get(first.id), store.get(stale.id)) == (first, stale)
    assert comms.registry.require("worker").goal == goal


def test_retained_schema_is_carried_under_maintenance_before_runtime_writes(fork_bus):
    """Only the installer carries missing objects and declared earlier names;
    runtime writes refuse a changed schema and resume after that carry."""
    comms, source, order = fork_bus
    store = comms.goals.todos
    retained = store.get("P40")
    with store.writing() as db:
        db.execute(f'DROP TABLE "{ProblemAllocation.declared_name}"')
        db.execute(f'DROP INDEX "{Todo.unit_index.name(Todo)}"')
        db.execute(f'DROP INDEX "{Todo.problem_index.name(Todo)}"')
    with pytest.raises(RelationViolationError, match=HeldBusFile.carry_verb):
        store.allocate_problem()
    barrier = MaintenanceBarrier(comms.registry.store.path)
    hold = barrier.begin(MaintenanceOperator.current())
    with hold.holding():
        assert store.reconcile_at(store.path)
    barrier.clear(hold)
    assert store.allocate_problem() == "P41"
    assert store.get("P40") == retained
    with store.writing() as db:
        db.execute(f'ALTER TABLE "{ProblemAllocation.declared_name}" RENAME TO problem_number')
        db.execute(f'DROP INDEX "{Todo.problem_index.name(Todo)}"')
    with pytest.raises(RelationViolationError, match=HeldBusFile.carry_verb):
        store.allocate_problem()
    hold = barrier.begin(MaintenanceOperator.current())
    with hold.holding():
        assert store.reconcile_at(store.path)
    barrier.clear(hold)
    assert store.allocate_problem() == "P42"
    with store.snapshot() as db:
        assert [row.number for row in ProblemAllocation.select(db)] == [41, 42]
    assert store.get("P40") == retained


def test_created_ids_live_only_on_todos_and_allocation_uses_numeric_index(fork_bus):
    created_goal_id = GoalId.mint()
    direct_goal_id = GoalId.mint()
    comms, source, order = fork_bus
    store = comms.goals.todos
    lead = comms.registry.require("lead")
    retained = store.get("P40")
    issued = store.allocate_problem()
    assert issued == "P41"
    created = store.create(
        issued,
        source.github,
        "Create a real problem",
        creator=lead,
        assigner=lead.incarnation,
        owner=comms.registry.require("other"),
        generation=created_goal_id,
        request=retained.request.require(),
        review_at=retained.review_at,
        order=order,
    )
    with store.snapshot() as db:
        assert ProblemAllocation.select(db) == []
    # Direct creation must affect allocation without copying its key into the allocator.
    store.create(
        "P120",
        source.github,
        "Exercise numeric ordering",
        creator=lead,
        assigner=lead.incarnation,
        owner=comms.registry.require("worker"),
        generation=direct_goal_id,
        request=retained.request.require(),
        review_at=retained.review_at,
        order=replace(order, id=OrderId.mint()),
    )
    assert store.allocate_problem() == "P121"
    with store.snapshot() as db:
        assert [row.number for row in ProblemAllocation.select(db)] == [121]
        plan = db.execute(
            f"EXPLAIN QUERY PLAN SELECT max({Todo.problem_index.term_sql(Todo)}) "
            f'FROM "{Todo.declared_name}"'
        ).fetchall()
    print("Numeric identity query plan:", [tuple(row) for row in plan])
    assert store.get(issued) == created


def test_malformed_problem_ids_are_refused_without_writes(fork_bus):
    comms, source, order = fork_bus
    store = comms.goals.todos
    retained = store.get("P40")
    for text in ("P0", "X1", "broken", "1", "P01", "P-1", "P+1", "P١", "P1.0"):
        assert not ProblemNumber.accepts(text)
        with store.writing() as db, pytest.raises(TodoError) as refused:
            ProblemAllocation.reserve(db, text)
        assert text in str(refused.value)
    with store.snapshot() as db:
        assert ProblemAllocation.select(db) == []
    assert store.list(source.github) == (retained,)


def test_fork_superseded_verdict_reads_its_commanders_orders(fork_bus):
    worker_goal_id = GoalId.mint()
    comms, source, order = fork_bus
    store = comms.goals.todos
    lead = comms.registry.require("lead")
    store.create(
        "P41",
        source.github,
        "Replacing parent problem",
        creator=lead,
        owner=comms.registry.require("worker"),
        assigner=lead.incarnation,
        generation=worker_goal_id,
        review_at=RecordedEventTime(time.time() + 600),
        order=replace(order, id=OrderId.mint()),
        request=store.get("P40").request.require(),
    )
    comms.registry.register_child("fork-one", "lead", CommanderForkExecution, comms.goals)
    actor = CommanderForkCaller("fork-one").identity(comms)
    current = store.get("P40")
    verdict = SupersededVerdict(
        expected_revision=current.revision,
        actor=actor,
        previous=current.held(),
        tool_call_id=ToolCallId("retained-edit"),
        replacement="P41",
        reason="The replacing order moved this edit to another unit.",
    )
    assert verdict.prepare(comms.goals, current) == verdict
    unknown = replace(verdict, replacement="P99")
    with pytest.raises(ValueError, match="not held for this commander"):
        unknown.prepare(comms.goals, current)


@pytest.fixture
def subagent_hook(fork_bus, tmp_path, monkeypatch, capsys):
    """Runs a hook program as Claude Code does, against this bus through the real command."""
    comms = fork_bus[0]
    scripts = tmp_path / "commander-bin"
    scripts.mkdir()
    script = scripts / "agent-comms-commander"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} -c 'from agent_comms.commander.cli"
                      f" import main; main()' --root {comms.root} \"$@\"\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{scripts}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    transcript = tmp_path / "commander.jsonl"
    WatchClaim("commander").enrol(WatchRecord("lead", str(comms.root), (), tmp_path / "state"))

    def run(program, event, **stop):
        record = {"session_id": "commander", "hook_event_name": event,
                  "transcript_path": str(transcript), "agent_id": "a1",
                  "agent_type": "general-purpose", **stop}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(record)))
        program()
        return capsys.readouterr().out

    return comms, run


def test_a_subagent_start_registers_its_fork_and_a_resume_reuses_the_archived_thread(
        subagent_hook, tmp_path):
    comms, run = subagent_hook
    told = json.loads(run(forks.started, "SubagentStart"))["hookSpecificOutput"]
    assert "--fork claude-a1" in told["additionalContext"]
    fork = comms.registry.require("claude-a1")
    assert fork.parent.named("lead") and comms.registry.status("claude-a1").receiving
    assert comms.agents.activity.current(fork.name).state is ActivityState.THINKING
    assert run(read_gate.main, "PreToolUse", tool_name="Bash", tool_input={"command": "pwd"}) == ""
    activity = comms.agents.activity.current(fork.name)
    assert (activity.state, activity.detail) == (ActivityState.WORKING, "Bash")
    found = tmp_path / "found.txt"
    found.write_text("The fork recorded its finding.")
    assert command(comms, "order", "say", "#comms", str(found), "--fork", "claude-a1").returncode == 0
    assert run(forks.stopped, "SubagentStop", stop_hook_active=False) == ""
    assert comms.agents.activity.current(fork.name).state is ActivityState.IDLE
    run(forks.started, "SubagentStart")
    assert comms.registry.require("claude-a1").incarnation == fork.incarnation
    assert comms.registry.status("claude-a1").receiving


def test_a_subagent_stop_is_refused_until_its_fork_records_then_archives_it(
        subagent_hook, tmp_path):
    comms, run = subagent_hook
    run(forks.started, "SubagentStart")
    assert json.loads(run(forks.stopped, "SubagentStop", stop_hook_active=False))["decision"] == "block"
    assert comms.registry.status("claude-a1").receiving
    found = tmp_path / "found.txt"
    found.write_text("The fork recorded its finding.")
    assert command(comms, "order", "say", "#comms", str(found), "--fork", "claude-a1").returncode == 0
    assert run(forks.stopped, "SubagentStop", stop_hook_active=False) == ""
    assert isinstance(comms.registry.status("claude-a1"), ArchivedThreadStatus)


def test_a_sent_back_stop_ends_a_fork_that_recorded_nothing_and_reports_it_missing(subagent_hook):
    """Claude Code ends the fork on the stop after a send-back; it must not stay subscribed."""
    comms, run = subagent_hook
    run(forks.started, "SubagentStart")
    assert json.loads(run(forks.stopped, "SubagentStop", stop_hook_active=False))["decision"] == "block"
    assert run(forks.stopped, "SubagentStop", stop_hook_active=True) == ""
    assert isinstance(comms.registry.status("claude-a1"), ArchivedThreadStatus)
    report, = comms.bus.bus.latest_addressed(frozenset({"claude-a1"}), frozenset({"lead"}))
    assert "stopped without recording what it found" in report.body


def test_a_commander_session_end_ends_each_subagent_core_still_has(subagent_hook, tmp_path):
    """A subagent killed without a SubagentStop ends with its session, once."""
    comms, run = subagent_hook
    run(forks.started, "SubagentStart")
    subagents = tmp_path / "commander" / "subagents"
    subagents.mkdir(parents=True)
    for agent in ("a1", "never-joined"):
        (subagents / f"agent-{agent}.jsonl").write_text("")
    session = forks.SessionSubagents.decode({
        "session_id": "commander", "hook_event_name": "SessionEnd",
        "transcript_path": str(tmp_path / "commander.jsonl")})
    for _ in range(2):
        assert session.end_subagents() == ()
        assert isinstance(comms.registry.status("claude-a1"), ArchivedThreadStatus)
    assert "claude-never-joined" not in comms.registry
    report, = comms.bus.bus.latest_addressed(frozenset({"claude-a1"}), frozenset({"lead"}))
    assert "its work is missing" in report.body


def test_a_unit_sessions_subagents_take_role_slots_and_an_unbound_sessions_take_none(
        fork_bus, tmp_path, monkeypatch, capsys):
    """A subagent of a bound session that is not commander sits in the first vacant slot of
    its role under that session's thread, never a commander fork; a slot it leaves keeps its
    thread and history for the next occupant. A session bound to nothing has no bus to join."""
    comms = fork_bus[0]
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    transcript = tmp_path / "unit.jsonl"

    def run(program, event, agent_id, **tool):
        record = {"session_id": "unit", "hook_event_name": event, "transcript_path": str(transcript),
                  "agent_id": agent_id, "agent_type": "Explore", "stop_hook_active": False, **tool}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(record)))
        program()
        return capsys.readouterr().out

    assert run(forks.started, "SubagentStart", "a2") == ""
    assert not comms.registry.snapshot().children("worker")
    WatchClaim("unit").enrol(WatchRecord("worker", str(comms.root), (), tmp_path / "state"))

    told = json.loads(run(forks.started, "SubagentStart", "a2"))["hookSpecificOutput"]
    assert "slot worker-explore" in told["additionalContext"]
    slot = comms.registry.require("worker-explore")
    assert slot.parent.named("worker") and comms.registry.status("worker-explore").receiving
    slot.execution.require_subagent()
    assert comms.agents.activity.current(slot.name).state is ActivityState.THINKING
    assert run(read_gate.main, "PreToolUse", "a2", tool_name="Read",
               tool_input={"file_path": str(transcript)}) == ""
    activity = comms.agents.activity.current(slot.name)
    assert (activity.state, activity.detail) == (ActivityState.WORKING, "Read")
    # A long-running tool stays working beyond the activity store's ordinary stale budget.
    comms.agents.activity.emit(replace(activity, timestamp=1.))
    snapshot = comms.registry.snapshot()
    assert comms.agents.activity_of(slot.name, snapshot=snapshot).state is ActivityState.WORKING
    with pytest.raises(RelationViolationError, match="not a declared commander fork"):
        slot.execution.require_commander_fork()
    assert "claude-a2" not in comms.registry
    run(forks.started, "SubagentStart", "a3")
    assert comms.registry.require("worker-explore-2").parent.named("worker")

    assert run(forks.stopped, "SubagentStop", "a2") == ""
    assert isinstance(comms.registry.status("worker-explore"), ArchivedThreadStatus)
    assert comms.agents.activity.current(slot.name).state is ActivityState.IDLE
    assert comms.registry.status("worker-explore-2").receiving
    run(forks.started, "SubagentStart", "a4")
    assert comms.registry.require("worker-explore").incarnation == slot.incarnation
    assert comms.registry.status("worker-explore").receiving

    session = forks.SessionSubagents.decode({
        "session_id": "unit", "hook_event_name": "SessionEnd", "transcript_path": str(transcript)})
    assert session.end_subagents() == ()  # a3 never stopped: its session's end vacates it
    assert isinstance(comms.registry.status("worker-explore-2"), ArchivedThreadStatus)
    assert isinstance(comms.registry.status("worker-explore"), ArchivedThreadStatus)
    assert not any(session.seats.iterdir())


def test_a_subagent_role_names_its_slots_in_thread_name_characters():
    assert SubagentRole("Explore").slug == "explore"
    assert SubagentRole("plugin:Code Review").slug == "plugin-code-review"
    names = SubagentRole("Explore").slot_names("worker")
    assert [next(names) for _ in range(3)] == ["worker-explore", "worker-explore-2", "worker-explore-3"]
    with pytest.raises(ValueError, match="no letters or digits"):
        SubagentRole("::")


def test_only_a_subagent_slot_is_retired_and_a_stopped_stranger_is_no_slot(fork_bus):
    """Retiring archives only a declared child; a stopped thread that happens to carry a
    role slot's name is refused, not adopted."""
    comms = fork_bus[0]
    with pytest.raises(RelationViolationError, match="not a declared subagent"):
        SubagentCaller("worker").retire(comms)
    comms.registry.declare(Thread("other-explore", frozenset(), Checkout(Path("/tmp")),
                                  parent=ForkedParent("other")), status=StoppedThreadStatus())
    with pytest.raises(RelationViolationError, match="not a declared subagent"):
        comms.registry.claim_slot("other", SubagentRole("Explore"), comms.goals)
