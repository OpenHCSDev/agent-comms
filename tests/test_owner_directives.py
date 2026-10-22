"""The owner's own messages and claude-ai's are both directives on a thread holding owner
requests, recorded once by the origin that speaks for the owner."""

from agent_comms.connector import DirectorCaller
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import RequestDirective, RequestGoalOrder
from agent_comms.tools import invoke_tool
from goal_owner_fixture import canonical_goal_wire


def requesting_lead(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("lead", "claude-ai"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({"base"}), str(tmp_path)))
    comms.goals.update_goal("lead", SetGoalAction(
        text="Tristan's plans, in order", goal_id="request", order=RequestGoalOrder()))
    return comms


def overdue(comms, directive: RequestDirective):
    goal = comms.registry.require("lead").goal
    owner = comms.registry.require("lead").incarnation
    return tuple(due for due, _original in goal.order.overdue_directives(
        goal, comms.goals, owner, directive.deadline + 1))


def test_the_owners_message_is_a_directive_the_overdue_rule_reads(tmp_path):
    comms = requesting_lead(tmp_path)
    message = comms.messaging.send_user_message("lead", "Send the order now.", worktree=str(tmp_path))
    directives = comms.registry.require("lead").goal.order.directives
    assert [directive.seq for directive in directives] == [message.seq]
    assert overdue(comms, directives[0]) == directives


def test_an_owner_message_to_a_channel_records_no_directive(tmp_path):
    comms = requesting_lead(tmp_path)
    comms.channels.create_tag("base")
    comms.messaging.send_user_message("#base", "Everyone, stop now.", worktree=str(tmp_path))
    assert comms.registry.require("lead").goal.order.directives == ()


def test_claude_ai_send_records_exactly_one_directive(tmp_path):
    comms = requesting_lead(tmp_path)
    invoke_tool(comms, "comms_send",
                {"from": "claude-ai", "to": "lead", "body": "Send the order now."},
                caller=DirectorCaller("claude-ai"))
    directives = comms.registry.require("lead").goal.order.directives
    assert len(directives) == 1
    assert overdue(comms, directives[0]) == directives
