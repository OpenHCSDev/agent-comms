"""Commander forks use registered identity and the original Todo assignment."""

import io
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.change_report import FileLine
from agent_comms.commander.order import CommanderContext, OrderFile
from agent_comms.commander.standing import Unjudged
from agent_comms.comms import wire
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import FieldCodec, GoalId, OrderId
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.installer.backup import StoreBackup
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceNotHeldError,
    MaintenanceOperator,
)
from agent_comms.native_tool_call import ToolCallId
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.order_tools import CommsCommanderLoadTool, CommsHudTool, CommsTodoListTool
from agent_comms.problem_number import ProblemNumber
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.slop import DeclarationDerivation, ExistingLine, Slop
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import ArchivedThreadStatus
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread
from agent_comms.errors import TodoError
from agent_comms.todos import (
    PendingProblemOrder,
    ProblemAllocation,
    ReleaseTodo,
    StrikeReason,
    SupersededVerdict,
    Todo,
)
from agent_comms.tool_caller import CommanderForkCaller
from agent_comms_hooks import forks
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.transcript_events import ToolStartTranscript
from agent_comms.turn_item import NativeItemId, NativeTurnId
from cli_command import commander
from source_revision_fixture import SourceRepository


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
                str(worktree),
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
    return commander("--root", str(comms.root), *args)


def process_command(comms, *args):
    """Independent processes must resolve identity and acquire the same Todo write lock."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_comms.commander.cli import main; main()",
            "--root",
            str(comms.root),
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
        store.require_unit_in(db, saved.held().owner_identity, reissuer)
    print(f"Core send actor: {saved.held().parent}; problem commander: {saved.creator}")
    ctx = CommanderContext.read_root(comms.root).model_copy(
        update={"actor": CommanderForkCaller(fork.name)}
    )
    hud = ctx.call(CommsHudTool(thread=ctx.commander))
    assert hud.commander == parent.incarnation
    assert [(unit.unit, tuple(order.problem for order in unit.orders)) for unit in hud.units] == [
        (saved.held().owner_identity, ("P40",))
    ]
    assert set(ctx.model_copy(deep=True).progress()) == {"P40"}
    assert ctx.held_orders() == (saved,)
    assert ctx.call(CommsTodoListTool(thread=ctx.commander)).todos == (saved,)
    load = ctx.call(CommsCommanderLoadTool(thread=ctx.commander))
    assert load.owed == 1 and load.timers_up == 0 and load.unjudged == 0
    assert "unjudged edits" in str(load)
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
        store.require_unit_in(db, saved.held().owner_identity, parent.incarnation)


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
    from agent_comms.held_orders import HeldOrders
    from agent_comms.todos import MergedProgress, NoCommitProgress
    from agent_comms.turn_digests import OrderEditCountRead

    comms, source, _ = fork_bus
    record_edits(comms, source, 3)
    holds = HeldOrders.read(comms, "lead").live
    open_order = {hold.todo.id: NoCommitProgress() for hold in holds}
    merged = {hold.todo.id: MergedProgress("a" * 40) for hold in holds}
    assert [read.read() for read in OrderEditCountRead.judgeable(
        comms.root, holds, open_order)] == [3]
    assert tuple(OrderEditCountRead.judgeable(comms.root, holds, merged)) == ()


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
    with store.snapshot() as db:
        store.require_unit_in(db, owner, todo.held().parent_identity)
        store.require_unit_in(db, other_birth, comms.registry.require("other").incarnation)
        queries = []
        db.set_trace_callback(queries.append)
        with pytest.raises(TodoError, match="retained is assigned by lead"):
            store.require_unit_in(db, owner, comms.registry.require("other").incarnation)
        db.set_trace_callback(None)
        plan = db.execute("EXPLAIN QUERY PLAN " + queries[0]).fetchall()
        print("Shared open-unit query plan:", [tuple(row) for row in plan])


def test_a_sibling_fork_assigns_a_unit_whose_order_waits_on_install(fork_bus):
    """Merged work waits only on its run, so its holder is free for any assigner."""
    comms, source, order = fork_bus
    store = comms.goals.todos
    todo = store.get("P40")
    owner = todo.held().owner_identity
    sibling = comms.registry.require("other").incarnation
    with store.snapshot() as db, pytest.raises(TodoError, match="finish or release"):
        store.require_unit_in(db, owner, sibling)
    with store.writing() as db:
        replace(todo, order=todo.order.merged("0" * 40), revision=todo.revision + 1).upsert(db)
    assert not store.get("P40").order.occupies_holder()
    with store.snapshot() as db:
        store.require_unit_in(db, owner, sibling)


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
    assert f"is assigned by {held[0].held().parent}" in refused.stderr
    assert "Traceback" not in refused.stderr, refused.stderr
    print(refused.stderr.strip())


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
    with pytest.raises(MaintenanceNotHeldError):
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
    with pytest.raises(MaintenanceNotHeldError):
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
    comms.registry.register_commander_child("fork-one", "lead", comms.goals)
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
    transcript.with_suffix(".reread").mkdir()

    def run(program, event):
        record = {"session_id": "commander", "hook_event_name": event,
                  "transcript_path": str(transcript), "agent_id": "a1"}
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
    found = tmp_path / "found.txt"
    found.write_text("The fork recorded its finding.")
    assert command(comms, "order", "say", "#comms", str(found), "--fork", "claude-a1").returncode == 0
    assert run(forks.stopped, "SubagentStop") == ""
    run(forks.started, "SubagentStart")
    assert comms.registry.require("claude-a1").incarnation == fork.incarnation
    assert comms.registry.status("claude-a1").receiving


def test_a_subagent_stop_is_refused_until_its_fork_records_then_archives_it(
        subagent_hook, tmp_path):
    comms, run = subagent_hook
    run(forks.started, "SubagentStart")
    assert json.loads(run(forks.stopped, "SubagentStop"))["decision"] == "block"
    assert comms.registry.status("claude-a1").receiving
    found = tmp_path / "found.txt"
    found.write_text("The fork recorded its finding.")
    assert command(comms, "order", "say", "#comms", str(found), "--fork", "claude-a1").returncode == 0
    assert run(forks.stopped, "SubagentStop") == ""
    assert isinstance(comms.registry.status("claude-a1"), ArchivedThreadStatus)
