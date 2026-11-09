"""The owner's own messages and claude-ai's are both directives on a thread holding owner
requests, recorded once by the origin that speaks for the owner."""

import pytest

from agent_comms.transport.connector import ConnectorSendTool
from agent_comms.errors import CommittedMessageError
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.planning.owner_requests import RequestDirective
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.typed_table import RowNotFoundError
from agent_comms.transport.connector_caller import DirectorCaller
from agent_comms.transport.tools.served import invoke_tool
from goal_owner_fixture import canonical_goal_wire


def requesting_lead(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("colleague", "claude-ai"):
        comms.registry.declare(NativeThread(name, frozenset({"base"}), Checkout(tmp_path)))
    comms.goals.todos.add_request(
        comms.registry.require("colleague").incarnation, "Tristan's plans, in order")
    return comms


def overdue(comms, directive: RequestDirective):
    request, = comms.goals.todos.open_requests(comms.registry.require("colleague").incarnation)
    with comms.bus.bus.reading() as source:
        return tuple(due for due, _original in request.directive_messages(
            source, comms.registry.require("colleague").incarnation.require_recorded().lookup)
            if due.deadline < directive.deadline + 1)


def test_the_owners_message_is_a_directive_the_overdue_rule_reads(tmp_path):
    comms = requesting_lead(tmp_path)
    message = comms.messaging.send_user_message(CommandTarget.named('colleague'), "Send the order now.", worktree=Checkout(tmp_path))
    request, = comms.goals.todos.open_requests(comms.registry.require("colleague").incarnation)
    directives = request.directives
    assert [directive.seq for directive in directives] == [message.seq]
    assert overdue(comms, directives[0]) == directives


def test_an_owner_message_to_a_channel_records_no_directive(tmp_path):
    comms = requesting_lead(tmp_path)
    comms.channels.create_tag("base")
    comms.messaging.send_user_message(CommandTarget.named('#base'), "Everyone, stop now.", worktree=Checkout(tmp_path))
    request, = comms.goals.todos.open_requests(comms.registry.require("colleague").incarnation)
    assert request.directives == ()


def test_claude_ai_send_records_exactly_one_directive(tmp_path):
    comms = requesting_lead(tmp_path)
    invoke_tool(comms, "comms_send",
                {"to": "colleague", "body": "Send the order now."},
                caller=DirectorCaller("claude-ai"))
    request, = comms.goals.todos.open_requests(comms.registry.require("colleague").incarnation)
    directives = request.directives
    assert len(directives) == 1
    assert overdue(comms, directives[0]) == directives


def test_connector_keeps_the_selected_request_when_notification_fails(tmp_path):
    comms = requesting_lead(tmp_path)
    store = comms.goals.todos
    original, = store.open_requests(comms.registry.require("colleague").incarnation)
    newer = store.add_request(original.teammate, "An independent newer request")
    signal, = comms.bus.bus.source_paths(comms.root)
    signal.unlink(missing_ok=True)
    signal.parent.mkdir(exist_ok=True)
    signal.mkdir()
    with pytest.raises(CommittedMessageError) as failed:
        ConnectorSendTool(
            target="colleague", request=original.id,
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
        message = comms.messaging.send_user_message(CommandTarget.named('colleague'), "Record the requested work", worktree=Checkout(tmp_path))
        request, = comms.goals.todos.open_requests(comms.registry.require("colleague").incarnation)
        comms.goals.todos.record_directive(request.id, RequestDirective(
            message.seq, message.timestamp + RequestDirective.window + 1))
        order = comms.goals.todos.owner_request(request.id)
        assert len(order.directives) == 2
        with pytest.raises(RowNotFoundError):
            order.directive_messages(
                older, comms.registry.require("colleague").incarnation.require_recorded().lookup)
    with comms.bus.bus.reading() as source:
        assert tuple(directive for directive, _ in order.directive_messages(
            source, comms.registry.require("colleague").incarnation.require_recorded().lookup)) == order.directives
