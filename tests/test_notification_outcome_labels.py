"""Read proof survives an independent attempt outcome; neither fact implies a reply."""
import pytest

from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.message_notification import MessageNotification
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_progress import (
    AnsweredResponse, CompletedProgress, ConsumedDelivery, ConsumingProgress,
    FailedProgress, InterruptedProgress, PassiveProgress, SentProgress, ShownToModelProgress,
)
from agent_comms.nominal.field_codec import MessageId, ThreadLookup

LOOKUP = ThreadLookup('0' * 32)
AT = 1000


def notice(progress):
    return MessageNotification(FrozenRecipient(LOOKUP, 'fixture'), LOOKUP, progress)


@pytest.mark.parametrize('wrapper', [FailedProgress, InterruptedProgress, CompletedProgress, PassiveProgress])
@pytest.mark.parametrize('read', [False, True])
def test_attempt_label_preserves_original_context_entry(wrapper, read):
    proof = ShownToModelProgress(AT) if read else SentProgress(AT)
    notification = notice(wrapper(proof))
    assert notification.state.is_consumed() is read
    assert notification.state.proof is proof
    if read:
        original_read = notification.timed_state(ConsumedDelivery.label, AT)
        assert notification.label == f'{original_read} · {wrapper.label.lower()}'
    else:
        assert notification.label == wrapper.label
    assert notification.answer.detail_texts() == ()


def test_nested_outcomes_keep_one_read_clock_without_claiming_live_work():
    proof = ConsumingProgress(AT)
    notification = notice(FailedProgress(FailedProgress(InterruptedProgress(proof))))
    original_read = notification.timed_state(ConsumedDelivery.label, AT)
    assert notification.label == f'{original_read} · failed'
    assert notification.state.is_consumed()
    assert 'Answering' not in notification.label
    assert notification.state.proof.proof.proof is proof


def test_independent_answer_keeps_its_own_identity_and_time():
    notification = notice(FailedProgress(ShownToModelProgress(AT)))
    response = AnsweredResponse(MessageReference(123, MessageId('1' * 12)), 2000)
    answered = response.answered(notification)
    assert answered.answer is response
    assert answered.label == answered.timed_state('Answered', response.received_at_ms)
    assert answered.state.proof is notification.state
    assert answered.state.is_consumed()
