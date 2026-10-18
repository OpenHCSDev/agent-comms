"""Every declared tool's reply is the encoding of its declared result record."""
import os
from dataclasses import replace
from typing import get_type_hints

from agent_comms.change_report import FileLine
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, PausedGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.plan_phases import RecordPlanePhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (AcceptedVerdict, MechanicalOrderRun, OperationDelivery, PendingProblemOrder,
    StrikeTodo)
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller
from agent_comms.tool_result import ToolResult
from agent_comms.tools import (
    CommsArchiveTool, CommsCancelRestartTool, CommsChannelsTool, CommsCollaborationTool,
    CommsCollaborationsTool, CommsConstraintTool, CommsDecisionTool, CommsDeleteViewTool,
    CommsEditGoalTool, CommsGoalHistoryTool, CommsGoalTool, CommsInboxTool,
    CommsCorrectWordingTool, CommsFireTool, CommsModelTool, CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderServeTool, CommsOrderTool, CommsOrderWorkTool, CommsPinChannelTool, CommsPinThreadTool,
    CommsProblemCloseTool, CommsRenameSelfTool, CommsRestartQueueTool,
    CommsResumeGoalTool, CommsSendTool, CommsSetChannelMetadataTool, CommsSetGoalTool,
    CommsSetProjectTool, CommsSetViewTool, CommsSortChannelTool, CommsSortChannelsTool,
    CommsStopTool, CommsSubtaskTool, CommsTagsTool, CommsThreadTagsTool,
    CommsThreadsTool, CommsTodoCreateTool, CommsTodoListTool, CommsTodoTool, CommsTurnsTool, CommsVerdictTool,
    CommsWorkBranchesTool, LiveOwnerTool, ReadTool, ToolRequest,
)
from agent_comms.work_branches import WorkBranches


def scenario(comms: Comms, project, monkeypatch):
    """One call per declared tool but the live-owner tools, which test_start and
    test_restart_queue invoke against real processes; ordered so each call meets its preconditions."""
    lead, worker, nobody = ThreadCaller("lead"), ThreadCaller("worker"), NoThreadCaller()
    for created, name in enumerate(("lead", "worker", "idle"), 1):
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset(), str(project), created_at=float(created)))
    comms.goals.update_goal("worker", SetGoalAction(text="Original commander brief", goal_id="ongoing"))
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread(
        "author", frozenset({"team"}), str(project), process_identity=ProcessIdentity.capture(os.getpid())))
    owner, claim = comms.registry.live_owner_with_claim("author")
    comms.registry.lease_live_turn_with_claim(owner, "author-turn", expected_generation=claim)
    author = ThreadCaller("author")
    yield CommsThreadTagsTool, {"thread": "worker", "add": "team"}, lead
    yield CommsTagsTool, {}, lead
    yield CommsChannelsTool, {}, lead
    yield CommsPinChannelTool, {"name": "#team", "pinned": True}, lead
    yield CommsPinThreadTool, {"channel": "#team", "name": "worker", "pinned": True}, lead
    yield CommsSortChannelsTool, {"order": "created_at"}, lead
    yield CommsSetChannelMetadataTool, {"name": "#team", "parent": "", "archived": False}, lead
    yield CommsSortChannelTool, {"name": "#team", "order": "created_at"}, lead
    yield CommsSetViewTool, {"name": "crew", "kind": "participants", "match": "any_of", "tags": "team"}, lead
    yield CommsDeleteViewTool, {"name": "crew"}, lead
    yield CommsModelTool, {"model": "test/model", "thinking_level": "low"}, lead
    yield CommsSetProjectTool, {"path": str(project)}, lead
    yield CommsSetGoalTool, {"text": "Lead the team"}, lead
    goal = comms.registry.require("lead").goal
    yield CommsGoalTool, {"goal_id": goal.id, "status": "active", "progress": "Started"}, lead
    yield CommsEditGoalTool, {"goal_id": goal.id, "text": "Lead the whole team"}, lead
    comms.goals.update_goal("lead", PausedGoalAction(progress="Paused by its owner"))
    yield CommsResumeGoalTool, {"goal_id": goal.id, "progress": "Resumed"}, lead
    yield CommsGoalHistoryTool, {"thread": "lead"}, lead
    comms.goals.update_goal("lead", SetGoalAction(text="Owner request", goal_id="request", order=RequestGoalOrder()))
    order = PendingProblemOrder(
        id="order-P1", owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(comms.root), "printf 1", CountCheckResult(1)),
        edits=("Repair the existing owner",), banned=("No second store",),
        report="Report the result", at=RecordedEventTime(100),
        first_call_deadline=RecordedEventTime(220))
    yield CommsInboxTool, {"thread": "worker", "standby": {"goal_id": "ongoing", "wait_for": ["lead"]}}, worker
    yield CommsTodoCreateTool, {"owner": "worker", "order": FieldCodec.encode(CreateProblemGoalAction(
        problem="P1", order=order, request="request", deadline=RecordedEventTime(1000),
        commander="lead", repo="OpenHCSDev/agent-comms", text="Fix P1"))}, lead
    yield CommsTodoListTool, {"thread": "lead"}, lead
    yield CommsCorrectWordingTool, {"unit": "worker", "message_id": "m1",
                                    "text": "The canonical ownership changed"}, lead
    todo = comms.goals.todos.get("P1")
    actor = comms.registry.require("lead").incarnation
    yield CommsTodoTool, {"id": "P1", "operation": FieldCodec.encode(StrikeTodo(
        todo.revision, actor, todo.assignment, "The assigned change broke the existing resource release path during review.",
        RecordedEventTime(500)))}, lead
    yield CommsVerdictTool, {"id": "P1", "operation": FieldCodec.encode(AcceptedVerdict(
        todo.revision + 1, actor, todo.assignment, "toolu_1", quote="Repair the existing owner"))}, lead
    yield CommsOrderServeTool, {"problem": "P1", "serves": RecordPlanePhase.declared_name}, lead
    yield CommsOrderTool, {"owner": "worker", "order": FieldCodec.encode(OrderGoalAction(
        problem="P1", order=order, request="request", deadline=RecordedEventTime(3000),
        commander="lead", expected_revision=comms.goals.todos.get("P1").revision))}, lead
    yield CommsOrderCheckTool, {"problem": "P1", "check": FieldCodec.encode(
        OrderCheck(str(comms.root), "printf 1", CountCheckResult(1))),
        "why": "The recorded check counted the wrong file"}, lead
    yield CommsOrderWorkTool, {"problem": "P1"}, lead
    yield CommsOrderDoneTool, {"problem": "P1", "delivery": FieldCodec.encode(OperationDelivery())}, lead
    (comms.root / "installed-evidence").write_text("Verified installed path")
    yield CommsProblemCloseTool, {
        "problem": "P1", "works": "The installed application now keeps the order across returns.",
        "artifact": str(comms.root / "installed-evidence")}, lead
    yield CommsTodoCreateTool, {"owner": "worker", "order": FieldCodec.encode(CreateProblemGoalAction(
        problem="P2", order=replace(order, id="order-P2"), request="request", deadline=RecordedEventTime(4000),
        commander="lead", repo="OpenHCSDev/agent-comms", text="Fix P2"))}, lead
    yield CommsFireTool, {"unit": "worker",
                          "reason": "The unit broke the release path twice and missed both of its deadlines."}, lead
    yield CommsTurnsTool, {"thread": "worker"}, lead
    yield CommsThreadsTool, {}, lead
    yield CommsThreadsTool, {}, nobody
    monkeypatch.setattr(WorkBranches, "read", lambda self, snapshot: ())  # It asks gh.
    yield CommsWorkBranchesTool, {"checkout": str(project)}, lead
    yield CommsCollaborationTool, {"action": "add", "peer": "worker", "note": "Pairing"}, lead
    yield CommsCollaborationsTool, {"thread": "lead"}, lead
    yield CommsSendTool, {"from": "lead", "to": "worker", "body": "Hello @worker"}, lead
    yield CommsDecisionTool, {"to": "#team", "chosen": "Typed records", "rejected": ["Dicts"]}, author
    yield CommsConstraintTool, {"to": "#team", "text": "Never replay inputs"}, author
    yield CommsSubtaskTool, {"to": "#team", "text": "Type the replies", "completed": False}, author
    yield CommsInboxTool, {"thread": "worker"}, lead
    yield CommsRenameSelfTool, {"new_name": "captain"}, lead
    yield CommsRestartQueueTool, {"name": "worker"}, nobody
    yield CommsCancelRestartTool, {"name": "worker"}, nobody
    yield CommsStopTool, {"name": "idle"}, nobody
    yield CommsArchiveTool, {"name": "idle"}, nobody


def result_type(tool: type[ToolRequest]) -> type:
    return get_type_hints(tool.apply, localns={"Comms": Comms})["return"]


def test_every_tool_reply_decodes_as_its_declared_result(comms, tmp_path, monkeypatch):
    invoked = set()
    for tool, arguments, caller in scenario(comms, tmp_path, monkeypatch):
        reply = tool.invoke(comms, arguments, caller=caller)
        assert FieldCodec.encode(FieldCodec.decode(result_type(tool), reply)) == reply, tool.declared_name
        invoked.add(tool)
    assert invoked == {tool for tool in ToolRequest.members_with(ToolRequest)
                       if not issubclass(tool, LiveOwnerTool)}


def stores(comms: Comms) -> dict:
    """registry.json, the todo store and the goal store, byte for byte."""
    paths = (comms.root / "registry.json", comms.goals.todos.path,
             *(comms.root / "goal-private").rglob("*"))
    return {path: path.read_bytes() for path in paths if path.is_file()}


def test_a_read_tool_changes_no_store_for_any_caller(comms, tmp_path, monkeypatch):
    read = set()
    for tool, arguments, caller in scenario(comms, tmp_path, monkeypatch):
        if not issubclass(tool, ReadTool):
            tool.invoke(comms, arguments, caller=caller)
            continue
        before = stores(comms)
        for reader in (caller, NoThreadCaller()):
            tool.invoke(comms, arguments, caller=reader)
            assert stores(comms) == before, (tool.declared_name, reader)
        read.add(tool)
    assert read == set(ToolRequest.members_with(ReadTool))


def test_every_tool_declares_a_tool_result():
    for tool in ToolRequest.members_with(ToolRequest):
        assert issubclass(result_type(tool), ToolResult), tool.declared_name
