"""An external CLI's goal is the bus's own: Core reads it, changes it as the owner and wakes the
CLI through its inbox, where a native thread's goal is its live runtime's."""

import asyncio

import pytest

from agent_comms.acp_extension import GoalSnapshotReply, UpdateMetadata
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.goal_actions import ActiveGoalAction, ClearGoalAction, PausedGoalAction, SetGoalAction
from agent_comms.planning.goal_history import RecordedGoalHistory
from agent_comms.planning.goal_scope import AllGoals
from agent_comms.planning.goals import GoalReply, NoGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_presentation import (
    BusHeldThreadOwnerBinding,
    UnavailableThreadOwnerBinding,
)
from agent_comms.registry.thread_status import ConnectedThreadStatus, StoppedThreadStatus
from agent_comms.registry.threads import ExternalThread, NativeThread
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


def test_a_stale_revision_and_a_retry_are_refused_for_an_external_goal(team_bus, cli):
    goal = FieldCodec.decode(GoalReply, ask(team_bus, cli, SetGoalRuntimeRequest(thread="cli", text="Land it"))).goal
    with pytest.raises(Exception, match="changed|revision"):
        ask(team_bus, cli, UpdateGoalRuntimeRequest(
            thread="cli", goal_id=goal.id, expected_revision=goal.revision + 1, status=PausedGoalAction))
    with pytest.raises(RelationViolationError, match="native owner runtime"):
        ask(team_bus, cli, RetryGoalRuntimeRequest(thread="cli", goal_id=goal.id, expected_revision=goal.revision))
