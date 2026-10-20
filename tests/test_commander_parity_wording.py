"""The wording comparison calls the configured real owners, without fake replies."""

from agent_comms.commander_parity.wording import (
    OrderWordingComparison,
    PlainWordsComparison,
    selected_context,
)
from agent_comms.message_reference import MessageReference


def test_live_plain_word_comparison_observes_both_owners():
    result = PlainWordsComparison.read()
    assert result.passed
    assert "mismatches=0" in result.describe()
    assert "agent_comms.todos.PlainText" in result.describe()


def test_live_native_order_wording_is_not_verified():
    result = OrderWordingComparison.read()
    assert not result.passed
    assert result.describe()


def test_retained_soft_message_exposes_missing_native_enforcement():
    context = selected_context()
    reference = MessageReference(seq=5084, message_id="79cf1033ccda")
    source = context.comms.bus.log.messages_for_references((reference,))
    assert tuple(message.reference for message in source) == (reference,)
    result = OrderWordingComparison.compare_messages(context, source)
    assert not result.passed
    assert "mismatches=1" in result.describe()
    assert "current_refusals=1" in result.describe()
    assert "native_refusals=0" in result.describe()
    assert "ProblemOrderGoalAction.request_ref" in result.describe()
    assert "seq=5084" in result.describe()
