"""An external CLI's goal is the bus's own: Core reads it, changes it as the owner and wakes the
CLI through its inbox, where a native thread's goal is its live runtime's."""

import asyncio

import pytest

from agent_comms.acp_extension import GoalSnapshotReply, UpdateMetadata
from agent_comms.cli_bridges.antigravity import AntigravityBridge
from agent_comms.cli_bridges.antigravity_rule_gate import AntigravityRuleGate
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.codex import CodexBridge
from agent_comms.cli_bridges.goal_semantics import (
    AntigravityGoalHarness,
    ClaudeGoalHarness,
    CodexGoalHarness,
    ExternalGoalHarness,
    InboxGoalHarness,
)
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    ClearGoalAction,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goal_history import RecordedGoalHistory
from agent_comms.planning.goal_presentation import RunnableExecution, RunningExecution
from agent_comms.planning.goal_scope import AllGoals
from agent_comms.planning.goals import GoalReply, NoGoal, ProblemGoalOrder
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_presentation import (
    BusHeldThreadOwnerBinding,
    UnavailableThreadOwnerBinding,
)
from agent_comms.registry.thread_status import ConnectedThreadStatus, StoppedThreadStatus
from agent_comms.registry.threads import ExternalThread, ForkThread, NativeThread
from agent_comms.transport.runtime_requests import (
    EditGoalRuntimeRequest,
    GoalHistoryRuntimeRequest,
    RetryGoalRuntimeRequest,
    SetGoalRuntimeRequest,
    UpdateGoalRuntimeRequest,
)
from agent_comms.transport.tool_caller import ToolCaller
from agent_comms.transport.served_tools.orders import ServedOwnerGoalRead
from agent_comms.transport.tools.reads import OwnerGoalRead
from agent_comms.transport.served_tools.served import ServedCommsOwnerRequestTool
from agent_comms.ui_model.observation import ReadAnswered, ReadOwnerOperation


@pytest.fixture
def cli(team_bus: Comms) -> ExternalThread:
    thread = team_bus.registry.declare(ExternalThread(
        "cli", frozenset(), Checkout(team_bus.root.parent), process_identity=ProcessIdentity.current()))
    team_bus.registry.register(thread, ConnectedThreadStatus())
    return team_bus.registry.require("cli")


def ask(comms: Comms, thread, request) -> object:
    """What the owner's UI receives for one owner request (CommsOwnerRequestTool)."""
    return ServedCommsOwnerRequestTool(owner=thread.incarnation, request=request).answered(comms).payload


def bus_snapshot(comms: Comms) -> GoalSnapshotReply:
    return GoalSnapshotReply.from_snapshot(comms.goals.goal_snapshot("cli"), UpdateMetadata.of())


def test_the_bus_answers_for_an_external_owner_and_nobody_for_a_stopped_native_one(team_bus, cli):
    snapshot = team_bus.registry.snapshot()
    assert cli.answering_binding(snapshot.owner_binding("cli")) == BusHeldThreadOwnerBinding()
    native = team_bus.registry.declare(NativeThread("stopped", frozenset(), Checkout(team_bus.root.parent)))
    team_bus.registry.register(native, StoppedThreadStatus())
    native = team_bus.registry.require("stopped")
    lease = team_bus.registry.snapshot().owner_binding("stopped")
    assert native.answering_binding(lease) == lease == UnavailableThreadOwnerBinding()


def test_a_viewer_that_predates_the_bus_held_answer_still_decodes_an_external_presentation(team_bus, cli):
    """The phone APK and an installed TUI decode presentations with the codec they shipped
    with, which refuses an unknown binding member ("Unknown ThreadOwnerBinding name"). So the
    wire keeps the registry lease such a viewer has always read for a CLI ("unavailable"), and
    a current viewer derives the bus-held answer from the read thread's kind."""
    published = team_bus.views.thread_presentations(("cli",)).read("cli").published()
    wire = FieldCodec.encode(published)
    assert wire["binding"] == {"kind": "unavailable"}
    assert "bus_held" not in repr(wire)
    received = FieldCodec.decode(type(published), wire)
    assert received.binding == UnavailableThreadOwnerBinding()
    assert received.answering_binding() == BusHeldThreadOwnerBinding()


async def test_mcp_and_the_ui_read_an_external_owner_goal_from_the_bus(team_bus, cli):
    for change in (lambda: None,
                   lambda: team_bus.goals.update_goal("cli", SetGoalAction(text="Ship the CLI goal"))):
        change()
        async with asyncio.timeout(30):
            mcp = await asyncio.to_thread(
                ServedOwnerGoalRead.invoke, team_bus, {"thread": "cli"},
                caller=ToolCaller.from_actor("claude-code"))
            ui = await ReadOwnerOperation(str(team_bus.root), cli.incarnation, OwnerGoalRead()).answer(team_bus)
        assert mcp == FieldCodec.encode(bus_snapshot(team_bus))
        assert isinstance(ui, ReadAnswered)
        assert ui.value == bus_snapshot(team_bus)
        assert ui.value.meta.batch.updates == ()
    assert ui.value.goal.require().text == "Ship the CLI goal"


def test_the_owner_ui_sets_pauses_resumes_edits_and_clears_an_external_goal_and_wakes_the_cli(team_bus, cli):
    pending = lambda: team_bus.bus.pending_count("cli")
    set_reply = FieldCodec.decode(GoalReply, ask(team_bus, cli, SetGoalRuntimeRequest(thread="cli", text="Land it")))
    goal = set_reply.goal
    assert team_bus.registry.require("cli").goal.require() == goal
    woken = pending()
    assert woken >= 1, "an active goal set from the UI wakes the CLI through its inbox"

    def update(status) -> GoalSnapshotReply:
        current = team_bus.registry.require("cli").goal.require()
        return FieldCodec.decode(GoalSnapshotReply, ask(team_bus, cli, UpdateGoalRuntimeRequest(
            thread="cli", goal_id=current.id, expected_revision=current.revision, status=status)))

    paused = update(PausedGoalAction)
    assert paused == bus_snapshot(team_bus)
    assert pending() == woken, "a paused goal holds no turn, so it wakes nobody"
    resumed = update(ActiveGoalAction)
    assert resumed == bus_snapshot(team_bus)
    assert pending() > woken, "resuming wakes the CLI as a nudge does"
    current = team_bus.registry.require("cli").goal.require()
    edited = FieldCodec.decode(GoalSnapshotReply, ask(team_bus, cli, EditGoalRuntimeRequest(
        thread="cli", goal_id=current.id, expected_revision=current.revision, text="Land it today")))
    assert edited.goal.require().text == "Land it today"
    assert (edited.goal.require().id, edited == bus_snapshot(team_bus)) == (goal.id, True)
    history = FieldCodec.decode(RecordedGoalHistory, ask(
        team_bus, cli, GoalHistoryRuntimeRequest(thread="cli", goal_id=AllGoals())))
    assert history == team_bus.goals.recorded_history("cli", AllGoals())
    assert len(history.history) >= 4, "set, pause, resume and edit are recorded revisions"
    cleared = update(ClearGoalAction)
    assert cleared.goal == NoGoal()


def test_a_stale_revision_is_refused_and_retry_wakes_a_blocked_external_goal(team_bus, cli):
    goal = FieldCodec.decode(GoalReply, ask(team_bus, cli, SetGoalRuntimeRequest(thread="cli", text="Land it"))).goal
    with pytest.raises(Exception, match="changed|revision"):
        ask(team_bus, cli, UpdateGoalRuntimeRequest(
            thread="cli", goal_id=goal.id, expected_revision=goal.revision + 1, status=PausedGoalAction))
    blocked = team_bus.goals.update_goal(
        "cli", BlockedGoalAction(progress="waiting on credentials"),
    )
    woken = team_bus.bus.pending_count("cli")
    retried = FieldCodec.decode(GoalReply, ask(team_bus, cli, RetryGoalRuntimeRequest(
        thread="cli", goal_id=blocked.id, expected_revision=blocked.revision))).goal
    assert retried.id == goal.id
    assert retried.revision == blocked.revision + 1
    assert team_bus.bus.pending_count("cli") > woken, "retrying a blocked external goal wakes the CLI"


def test_external_goal_execution_reflects_live_activity_and_fork_inherits_only_real_goals(team_bus, cli):
    team_bus.goals.update_goal("cli", SetGoalAction(text="Fix real goal projection"))
    assert isinstance(team_bus.goals.goal_snapshot("cli").execution, RunnableExecution)
    team_bus.agents.set_activity("cli", ActivityState.WORKING, "running tests")
    assert isinstance(team_bus.goals.goal_snapshot("cli").execution, RunningExecution)
    team_bus.agents.set_activity("cli", ActivityState.IDLE)
    assert isinstance(team_bus.goals.goal_snapshot("cli").execution, RunnableExecution)

    # Forking a parent with NoGoal never manufactures generic "Coordinate assigned work for ..." text.
    idle_parent = team_bus.registry.declare(ExternalThread(
        "idle-parent", frozenset(), Checkout(team_bus.root.parent), process_identity=ProcessIdentity.current()))
    team_bus.registry.register(idle_parent, ConnectedThreadStatus())
    forked_empty = team_bus.registry.register_child("idle-fork", "idle-parent", ForkThread, team_bus.goals)
    assert forked_empty.goal == NoGoal()

    # Forking a parent with a real active goal inherits that real goal text.
    forked_active = team_bus.registry.register_child("active-fork", "cli", ForkThread, team_bus.goals)
    assert forked_active.goal.require().text == "Fix real goal projection"


def test_external_goal_harness_family_and_antigravity_goal_context_injection(team_bus, cli, tmp_path, monkeypatch):
    assert ClaudeBridge.goal_harness is ClaudeGoalHarness
    assert AntigravityBridge.goal_harness is AntigravityGoalHarness
    assert CodexBridge.goal_harness is CodexGoalHarness
    assert ExternalGoalHarness.member_named("inbox") is InboxGoalHarness
    assert ClaudeGoalHarness.supports_stop_continuation() and ClaudeGoalHarness.supports_context_injection()
    assert AntigravityGoalHarness.supports_stop_continuation() and AntigravityGoalHarness.supports_context_injection()
    assert not CodexGoalHarness.supports_stop_continuation() and not CodexGoalHarness.supports_context_injection()

    goal = team_bus.goals.update_goal("cli", SetGoalAction(text="Ship nominal external goal support"))
    cursor, rendered = AntigravityGoalHarness.render_goal_context(team_bus, team_bus.registry.require("cli"))
    assert cursor.goal_id == str(goal.id)
    assert "Ship nominal external goal support" in rendered
    assert "`/goal`" in rendered

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / ".agy-thread.json").write_text('{"thread": "cli"}', encoding="utf-8")
    gate = AntigravityRuleGate(
        transcript_path=tmp_path / "transcript.jsonl",
        artifact_dir=artifact,
        cwd=team_bus.root.parent,
        workspace_paths=(team_bus.root.parent,),
    )
    from contextlib import contextmanager
    @contextmanager
    def fake_wire():
        yield team_bus
    monkeypatch.setattr("agent_comms.cli_bridges.antigravity_rule_gate.client_wire", fake_wire)

    first = gate.inject_goal_context(force=False)
    assert len(first) == 1 and "Ship nominal external goal support" in first[0]
    assert gate.inject_goal_context(force=False) == ()
    team_bus.goals.update_goal("cli", PausedGoalAction())
    paused_delta = gate.inject_goal_context(force=False)
    assert len(paused_delta) == 1 and "Paused" in paused_delta[0]


def test_problem_goal_order_derives_text_from_assigned_todos():
    class FakeTodo:
        def __init__(self, pid: str, defect: str) -> None:
            self._pid = pid
            self._defect = defect

        def goal_summary(self) -> str:
            return f"{self._pid}: {self._defect}"

    derived = ProblemGoalOrder.derived_text((FakeTodo("P10", "Fix scroll jank"), FakeTodo("P11", "Fix tab close")))
    assert derived == "P10: Fix scroll jank\nP11: Fix tab close"
