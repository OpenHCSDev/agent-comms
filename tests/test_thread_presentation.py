"""Attention belongs to the original status, including working and progress members."""
import pickle
from dataclasses import replace

import pytest

from agent_comms.errors import RouteChangedError, UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import UnobservedInputDeliveryScope, UnresolvedInput
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.notification_progress import SendingProgress
from agent_comms.thread_presentation import (
    IdleThreadPresentationStatus,
    NoThreadPresentation,
    OtherRouteThread,
    SessionDisconnected,
    StatusPending,
    ThreadPresentationStatus,
    WorkingThreadPresentationStatus,
)
from agent_comms.ui_model.delivery import DeliveryView, ReadDeliveryView
from agent_comms.ui_model.status import ThreadStatusModel


@pytest.mark.parametrize("presentation", [
    NoThreadPresentation(), StatusPending().presentation("worker"),
])
def test_presentation_family_preserves_member_through_transport_and_paint(presentation):
    decoded = pickle.loads(pickle.dumps(presentation))
    assert decoded == presentation
    assert decoded.same_status(presentation)
    assert decoded.same_observation(presentation)
    painted = SessionDisconnected("viewer closed").over(decoded)
    assert type(painted) is type(decoded)
    assert painted.read_identity is decoded.read_identity
    assert painted.notifications == decoded.notifications
    assert painted.binding == decoded.binding


def test_missing_presentation_owns_source_absence_and_retired_status(comms):
    presentations = comms.views.thread_presentations(("missing",))
    absent = presentations.read("missing")
    assert absent == presentations.view("missing") == NoThreadPresentation()
    assert not absent.notification_activity_names()
    assert absent.label == ""
    painted = ThreadStatusModel.known("missing", absent)
    retired = ThreadStatusModel.retired("missing")
    assert isinstance(painted.presentation, NoThreadPresentation)
    assert painted.lines == retired.lines
    assert painted.summary == retired.summary
    assert painted.presentation.same_status(retired.presentation)
    assert painted.presentation.label == retired.presentation.label
    with pytest.raises(UnregisteredThreadError):
        painted.presentation.require("missing")
    readable = {}
    absent.collect_revision_input("missing", readable)
    assert readable == {}
    with pytest.raises(UnregisteredThreadError, match="Thread presentation unavailable: missing"):
        presentations.require("missing")


def test_other_route_status_presents_the_route_owners_explanation():
    row = ThreadStatusModel.other_route("x")
    assert row.name == "x"
    assert isinstance(row.status, OtherRouteThread)
    assert row.lines == row.summary == (RouteChangedError.reader_sentence,)
    assert row.presentation.title == "x"


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
    delivery = ReadDeliveryView(DeliveryOverview(
        inputs=(UnresolvedInput(
            sequence=None, target="worker", text="Unconfirmed input", input_id="unconfirmed", status="unknown",
        ),), historical_count=0, dismissed_historical_count=0, historical_inputs=(),
        current_scope=UnobservedInputDeliveryScope,
    ))
    shown = delivery.presentation(ordinary)
    assert shown.status.details == (delivery.summary,)
    assert shown.summary == (ordinary.summary if working else delivery.summary)
    failed = DeliveryView().failed(ValueError("Original read failure"))
    shown = failed.presentation(ordinary)
    assert shown.status.details == ("Original read failure",)
    assert shown.summary == (ordinary.summary if working else "Original read failure")
