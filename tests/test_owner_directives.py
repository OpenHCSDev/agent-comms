"""The owner's own messages and claude-ai's are both directives on a thread holding owner
requests, recorded once by the origin that speaks for the owner."""

from agent_comms.channel_targets import CommandTarget

import pytest

from agent_comms.connector import ConnectorSendTool, DirectorCaller
from agent_comms.errors import CommittedMessageError
from agent_comms.owner_requests import RequestDirective
from agent_comms.tools import invoke_tool
from agent_comms.typed_table import RowNotFoundError
from goal_owner_fixture import canonical_goal_wire
from agent_comms.threads import Thread


def requesting_lead(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("lead", "claude-ai"):
        comms.registry.declare(Thread(name, frozenset({"base"}), str(tmp_path)))
    comms.goals.todos.add_request(
        comms.registry.require("lead").incarnation, "Tristan's plans, in order")
    return comms


def overdue(comms, directive: RequestDirective):
    request, = comms.goals.todos.open_requests(comms.registry.require("lead").incarnation)
    with comms.bus.bus.reading() as source:
        return tuple(due for due, _original in request.directive_messages(
            source, comms.registry.snapshot().names_for("lead"))
            if due.deadline < directive.deadline + 1)


def test_the_owners_message_is_a_directive_the_overdue_rule_reads(tmp_path):
    comms = requesting_lead(tmp_path)
    message = comms.messaging.send_user_message(CommandTarget.named('lead'), "Send the order now.", worktree=str(tmp_path))
    request, = comms.goals.todos.open_requests(comms.registry.require("lead").incarnation)
    directives = request.directives
    assert [directive.seq for directive in directives] == [message.seq]
    assert overdue(comms, directives[0]) == directives


def test_an_owner_message_to_a_channel_records_no_directive(tmp_path):
    comms = requesting_lead(tmp_path)
    comms.channels.create_tag("base")
    comms.messaging.send_user_message(CommandTarget.named('#base'), "Everyone, stop now.", worktree=str(tmp_path))
    request, = comms.goals.todos.open_requests(comms.registry.require("lead").incarnation)
    assert request.directives == ()


def test_claude_ai_send_records_exactly_one_directive(tmp_path):
    comms = requesting_lead(tmp_path)
    invoke_tool(comms, "comms_send",
                {"from": "claude-ai", "to": "lead", "body": "Send the order now."},
                caller=DirectorCaller("claude-ai"))
    request, = comms.goals.todos.open_requests(comms.registry.require("lead").incarnation)
    directives = request.directives
    assert len(directives) == 1
    assert overdue(comms, directives[0]) == directives


def test_connector_keeps_the_selected_request_when_notification_fails(tmp_path):
    comms = requesting_lead(tmp_path)
    store = comms.goals.todos
    original, = store.open_requests(comms.registry.require("lead").incarnation)
    newer = store.add_request(original.commander, "An independent newer request")
    comms.bus.bus.path.with_suffix(".changed").mkdir()
    with pytest.raises(CommittedMessageError) as failed:
        ConnectorSendTool(
            sender="claude-ai", target="lead", request=original.id,
            body="Read the request owner and report the result.",
        ).apply(comms, DirectorCaller("claude-ai"))
    committed = failed.value.record.message
    assert comms.bus.bus.lookup(committed.seq) == committed
    assert committed.task.requests == (original.id,)
    assert store.owner_request(original.id).directives == (RequestDirective.from_message(committed),)
    assert store.owner_request(newer.id).directives == ()


def test_recorded_directives_keep_repeated_sequences_and_refuse_an_older_bus_cut(tmp_path):
    comms = requesting_lead(tmp_path)
    with comms.bus.bus.reading() as older:
        message = comms.messaging.send_user_message(CommandTarget.named('lead'), "Record the requested work", worktree=str(tmp_path))
        request, = comms.goals.todos.open_requests(comms.registry.require("lead").incarnation)
        comms.goals.todos.record_directive(request.id, RequestDirective(
            message.seq, message.timestamp + RequestDirective.window + 1))
        order = comms.goals.todos.owner_request(request.id)
        responders = comms.registry.snapshot().names_for("lead")
        assert len(order.directives) == 2
        with pytest.raises(RowNotFoundError):
            order.directive_messages(older, responders)
    with comms.bus.bus.reading() as source:
        assert tuple(directive for directive, _ in order.directive_messages(source, responders)) == order.directives
