"""Attention belongs to the original status, including working and progress members."""
from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.notification_progress import SendingProgress
from agent_comms.thread_presentation import (
    IdleThreadPresentationStatus, SessionDisconnected, ThreadPresentationStatus,
    WorkingThreadPresentationStatus,
)
from agent_comms.ui_model.delivery import InputDelivery
from agent_comms.ui_model.status import ThreadStatusModel


@pytest.mark.parametrize("original", [
    IdleThreadPresentationStatus(), WorkingThreadPresentationStatus(),
    SendingProgress(23), SessionDisconnected("original disconnect"),
])
def test_attention_preserves_the_original_member_and_fields(original):
    status = original.with_attention("Inbox unavailable").with_attention("Original failure")
    assert type(status) is type(original)
    assert status == replace(original, details=("Inbox unavailable", "Original failure"))
    assert status.working == original.working
    assert FieldCodec.decode(ThreadPresentationStatus, FieldCodec.encode(status)) == status
    assert status.without_work() == IdleThreadPresentationStatus(details=status.details)
    assert not original.details


@pytest.mark.parametrize("status", [
    IdleThreadPresentationStatus(), WorkingThreadPresentationStatus(), SendingProgress(23),
])
def test_status_renders_attention_without_hiding_work(status):
    status = status.with_attention("Inbox unavailable", "Original failure")
    shown = status.activity_presentation("worker", "working", "Running tool")
    assert shown.marker == ("⌛" if status.working else "!")
    assert shown.summary == ("Working · Running tool" if status.working else "Inbox unavailable")
    row = ThreadStatusModel.known("worker", shown)
    assert row.lines == (("Working · Running tool", "Inbox unavailable", "Original failure")
                         if status.working else ("Inbox unavailable", "Original failure"))
    assert row.status is status


@pytest.mark.parametrize("working", [False, True])
def test_input_delivery_keeps_its_explanation_and_original_work(working):
    status = WorkingThreadPresentationStatus() if working else IdleThreadPresentationStatus()
    ordinary = status.activity_presentation("worker", "thinking", "Actual work")
    delivery = InputDelivery(inputs=({"input_id": "unconfirmed"},))
    shown = delivery.presentation(ordinary)
    assert shown.status.details == (delivery.summary,)
    assert shown.summary == (ordinary.summary if working else delivery.summary)
    failed = InputDelivery().failed(ValueError("Original read failure"))
    shown = failed.presentation(ordinary)
    assert shown.status.details == ("Original read failure",)
    assert shown.summary == (ordinary.summary if working else "Original read failure")
