"""Every declared tool's reply is the encoding of its declared result record."""
from agent_comms.repository_name import RepositoryName
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
from agent_comms.rule_reviews import DrawnRuleReviewEntry
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller
from agent_comms.tool_result import ToolResult
from agent_comms.tools import (
    CommsArchiveTool, CommsCancelRestartTool, CommsChannelsTool, CommsCollaborationTool,
    CommsCollaborationsTool, CommsConstraintTool, CommsDecisionTool, CommsDeleteViewTool,
    CommsEditGoalTool, CommsFleetStoryTool, CommsGoalHistoryTool, CommsGoalTool, CommsInboxTool,
    CommsOrderStoryTool,
    CommsCorrectWordingTool, CommsFireTool, CommsModelTool, CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderServeTool, CommsOrderTool, CommsOrderWorkTool, CommsPinChannelTool, CommsPinThreadTool,
    CommsProblemCloseTool, CommsRenameSelfTool, CommsRestartQueueTool, CommsRuleReviewTool,
    CommsResumeGoalTool, CommsSendTool, CommsSetChannelMetadataTool, CommsSetGoalTool,
    CommsSetProjectTool, CommsSetViewTool, CommsSortChannelTool, CommsSortChannelsTool, CommsStandingRulesTool,
    CommsStopTool, CommsSubtaskTool, CommsTagsTool, CommsThreadTagsTool,
    CommsThreadsTool, CommsTodoCreateTool, CommsTodoListTool, CommsChangeProblemTool, CommsTurnsTool, CommsVerdictTool,
    CommsUnjudgedTool, CommsWorkBranchesTool, LiveOwnerTool, ReadTool, ToolRequest,
)
from agent_comms.work_branches import WorkBranches


def result_type(tool: type[ToolRequest]) -> type:
    return get_type_hints(tool.apply, localns={"Comms": Comms})["return"]


def stores(comms: Comms) -> dict:
    """registry.json, the todo store and the goal store, byte for byte."""
    paths = (comms.root / "registry.json", comms.goals.todos.path,
             *(comms.root / "goal-private").rglob("*"))
    return {path: path.read_bytes() for path in paths if path.is_file()}


def test_every_tool_declares_a_tool_result():
    for tool in ToolRequest.members_with(ToolRequest):
        from typing import get_args
        result = result_type(tool)
        assert all(issubclass(member, ToolResult) for member in (get_args(result) or (result,))), tool.declared_name
