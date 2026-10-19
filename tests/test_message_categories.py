"""Wire message roles, not their senders, decide which toggle hides them."""

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.messages import (
    Message, MessageRole, InfoKind, ConversationRole, NoticeRole,
    HistoricalConversationRole, HistoricalNoticeRole,
    MembershipConversationRole, MembershipNoticeRole, MembershipChange,
)
from agent_comms.thread_identity import AgentThreadRole, HumanThreadRole
from agent_comms.transcript_events import IncomingTranscript
from agent_comms.ui_model.message_category import (
    InboundCategory, OtherCategory, all_categories, AgentActivityBoundary,
    UserCategory, AgentCategory, ThinkingCategory, ToolCategory,
)


@pytest.mark.parametrize("role,category", [
    (ConversationRole(), InboundCategory),
    (NoticeRole(), OtherCategory),
    (HistoricalConversationRole(), InboundCategory),
    (HistoricalNoticeRole(), OtherCategory),
    (MembershipConversationRole(MembershipChange.JOINED), InboundCategory),
    (MembershipNoticeRole(MembershipChange.JOINED), OtherCategory),
])
@pytest.mark.parametrize("sender_role", [AgentThreadRole, HumanThreadRole])
def test_role_owns_wire_filter_category(role, category, sender_role):
    message = Message(sender="alice", target="#comms", body="Visible text", type=InfoKind,
                      sender_role=sender_role, role=role)
    event = IncomingTranscript(message=message, reader="bob")
    selected = all_categories() - {OtherCategory}
    assert event.category is category
    assert (event.category in selected) == (category is InboundCategory)
    assert event.category in all_categories()


@pytest.mark.parametrize("data,category", [({}, InboundCategory), ({"notice": True}, OtherCategory)])
def test_wire_notice_field_decodes_to_its_category(data, category):
    assert FieldCodec.decode(MessageRole, data).category is category


def test_activity_grouping_uses_the_same_category_declarations():
    boundary = AgentActivityBoundary()
    assert boundary.observe(ThinkingCategory)
    assert not boundary.observe(ToolCategory)
    assert not boundary.observe(UserCategory)
    assert boundary.observe(ToolCategory)
    boundary.reset()
    assert not boundary.observe(AgentCategory)
    assert not boundary.observe(ThinkingCategory)
