"""Attention belongs to the original status, including working and progress members."""
import pickle
from dataclasses import replace

import pytest

from agent_comms.errors import UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.notification_progress import SendingProgress
from agent_comms.thread_presentation import (
    IdleThreadPresentationStatus, NoThreadPresentation, SessionDisconnected,
    StatusPending, ThreadPresentationStatus, WorkingThreadPresentationStatus,
)
from agent_comms.ui_model.delivery import InputDelivery
from agent_comms.ui_model.status import ThreadStatusModel


@pytest.mark.parametrize("presentation", [
    NoThreadPresentation(), StatusPending().presentation("worker"),
])
def test_presentation_family_preserves_member_and_observation(presentation):
    decoded = pickle.loads(pickle.dumps(presentation))
    assert decoded == presentation
    assert decoded.same_status(presentation)
    assert decoded.same_observation(presentation)


def test_missing_presentation_owns_source_absence_and_retired_status(comms):
    presentations = comms.views.thread_presentations(("missing",))
    absent = presentations.read("missing")
    assert absent == presentations.view("missing") == NoThreadPresentation()
    assert not absent.notification_activity_names()
    assert absent.label == ""
    assert ThreadStatusModel.known("missing", absent) == ThreadStatusModel.retired("missing")
    readable = {}
    absent.collect_revision_input("missing", readable)
    assert readable == {}
    with pytest.raises(UnregisteredThreadError, match="Thread presentation unavailable: missing"):
        presentations.require("missing")


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


@pytest.mark.parametrize("replacement", [False, True])
def test_idle_attention_is_carried_once_through_idle_presentation(replacement):
    status = IdleThreadPresentationStatus().with_attention("Inbox unavailable")
    ordinary = status.activity_presentation("worker", "idle", "")
    shown = status.when_idle(ordinary, lambda current: (
        IdleThreadPresentationStatus().activity_presentation(current.title, "idle", "")
        if replacement else current
    ))
    assert shown.status.details == ("Inbox unavailable",)
    assert shown.summary == "Inbox unavailable"
    assert shown.marker == "!"


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
