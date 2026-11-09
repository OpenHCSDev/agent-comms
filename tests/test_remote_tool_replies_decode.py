"""A connector caller decodes each reply into its tool's declared result, so a tool a remote UI
calls must declare a concrete one. CommsUserSendTool once inherited the abstract ToolResult from
PublishingToolRequest: Core posted the owner's message, the remote Toad could not decode the
reply, and its composer stayed held as if the send had failed."""

from agent_comms.cli_verbs.cli_commands import ThreadArchivedResult
from agent_comms.messaging.channels import Channel
from agent_comms.messaging.send_result import MessagePublished
from agent_comms.transport.tool_declaration import ToolDeclaration
from agent_comms.transport.tool_request import HumanToolRequest, PublishingToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools.orders import (
    CommsBoardTool,
    TeamBoard,
)
from agent_comms.transport.tools.served import (
    CommsArchiveTool,
    CommsAuthoredTaskTool,
    CommsDecisionTool,
    TaskPublished,
)
from agent_comms.transport.tools.sidebar import (
    ChannelOrderSet,
    CommsSortChannelsTool,
    CommsSortChannelTool,
    CommsUserSendTool,
)


def test_every_publishing_and_human_tool_declares_a_concrete_result():
    abstract = sorted(
        tool.__name__
        for tool in ToolDeclaration.members_with(ToolDeclaration)
        if issubclass(tool, (PublishingToolRequest, HumanToolRequest))
        and tool.result_type() is ToolResult
    )
    assert abstract == []


def test_the_channel_sorts_a_remote_ui_calls_decode_their_replies():

    assert CommsSortChannelsTool.result_type() is ChannelOrderSet
    assert CommsSortChannelTool.result_type() is Channel


def test_a_result_declared_on_one_parent_refines_the_default_another_parent_inherits():

    # ThreadToolRequest's header leaves the result at the family default; the publishing parent
    # declares TaskPublished. The declared result wins on the tool and on its subclasses.
    assert CommsAuthoredTaskTool.result_type() is TaskPublished
    assert CommsDecisionTool.result_type() is TaskPublished


def test_a_human_send_reply_decodes_into_its_published_message():

    assert CommsUserSendTool.result_type() is MessagePublished


def test_a_result_declared_only_on_apply_is_the_one_a_remote_reply_decodes_into():

    # Neither header names a result; each tool's apply does. A remote board or archive reply
    # used to decode into the bare ToolResult, dropping every field the caller reads.
    assert CommsBoardTool.result_type() is TeamBoard
    assert CommsArchiveTool.result_type() is ThreadArchivedResult
