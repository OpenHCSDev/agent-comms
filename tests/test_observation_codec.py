"""Whole observation messages round-trip through FieldCodec, as a connector carries them."""

import pytest

from agent_comms.coordination.errors import (
    DeferredRead,
    ReadAgain,
    Refused,
    ReloadRequired,
    ReportedDeferredRead,
    StaleRevision,
    TranscriptChanged,
)
from agent_comms.errors import GoalRefused, RouteChangedError
from agent_comms.nominal.field_codec import ErrorRecord, FieldCodec, MalformedRecord, ReportedError
from agent_comms.planning.goals import (
    GoalChangedError,
    GoalConsentRequired,
    UnobservedGoalCheckpoint,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import Thread
from agent_comms.ui_model.observation import ReadRefused, UnreadableHistory
from agent_comms.ui_model.unread import (
    CappedReplyUnread,
    EmptyCappedReplyUnread,
    ExactUnread,
    IndexingUnread,
    ReplyUnread,
    UnobservedUnread,
    UnreadPresentation,
)
from agent_comms_hooks import ToolRefusedError


def round_trip[T](value: T, annotation: type[T]) -> T:
    return FieldCodec.decode_document(annotation, FieldCodec.encode_document(value, annotation))


def test_a_refused_read_arrives_as_the_same_refusal() -> None:
    sent = ReadRefused(4, Refused.invalid_params({"field": "x"}), "trace")
    received = round_trip(sent, ReadRefused)
    assert type(received.error) is Refused
    assert (received.request_id, received.error.code, received.error.message, received.error.data) == (
        4, Refused.INVALID_PARAMS, "Invalid params", {"field": "x"})


def test_a_declared_unreadable_history_arrives_equal() -> None:
    sent = ReadRefused(5, UnreadableHistory("/runtime", "bad record"), "trace")
    assert round_trip(sent, ReadRefused) == sent


def test_an_undeclared_error_is_reported_never_rebuilt() -> None:
    received = round_trip(ReadRefused(6, RouteChangedError("moved"), "trace"), ReadRefused)
    assert type(received.error) is ReportedError
    assert (received.error.kind, received.error.message) == ("agent_comms.errors.RouteChangedError", "moved")


def test_a_deferred_read_field_receives_a_deferred_read() -> None:
    received = round_trip(DeferredRead("busy"), DeferredRead)
    assert isinstance(received, DeferredRead) and type(received) is ReportedDeferredRead


def test_a_declared_error_of_the_wrong_type_is_refused() -> None:
    sent = FieldCodec.encode(ErrorRecord.of(Refused(1, "x")), ErrorRecord)
    with pytest.raises(MalformedRecord):
        FieldCodec.decode_document(DeferredRead, sent)


def test_a_narrowed_refusal_arrives_as_itself() -> None:
    received = round_trip(ReadRefused(7, GoalRefused("no"), "trace"), ReadRefused)
    assert type(received.error) is GoalRefused
    assert (received.error.code, received.error.message) == (Refused.INVALID_PARAMS, "no")


def test_a_goal_refusal_arrives_as_an_intentional_refusal_and_the_bare_carrier_does_not() -> None:
    """A UI tells a refusal from a crash by ToolRefusedError alone. A goal refusal is one even
    after the wire; Refused itself also carries real failures (INTERNAL_ERROR), so it is not."""
    received = round_trip(ReadRefused(7, GoalRefused("no"), "trace"), ReadRefused)
    assert isinstance(received.error, ToolRefusedError)
    assert not isinstance(Refused(Refused.INTERNAL_ERROR, "compaction failed"), ToolRefusedError)


@pytest.mark.parametrize("unread", [
    ExactUnread(3), ReplyUnread(2), CappedReplyUnread(9), EmptyCappedReplyUnread(),
    IndexingUnread(), UnobservedUnread(),
])
def test_every_unread_kind_round_trips(unread: UnreadPresentation) -> None:
    received = round_trip(unread, UnreadPresentation)
    assert type(received) is type(unread) and received == unread


def goal_refusals(comms) -> tuple[Exception, ...]:
    """Every goal refusal member, built the way Core builds it."""
    comms.registry.declare(Thread("worker", frozenset(), Checkout(comms.root)))
    worker = comms.registry.require("worker")
    return (GoalRefused("no"), GoalConsentRequired(worker, worker.incarnation),
            GoalChangedError(worker, UnobservedGoalCheckpoint()))


def test_every_goal_refusal_arrives_as_itself(comms) -> None:
    """A member that narrows the constructor still crosses: it is rebuilt from declared state."""
    for sent in goal_refusals(comms):
        received = round_trip(ReadRefused(9, sent, "trace"), ReadRefused).error
        assert type(received) is type(sent)
        assert (received.code, received.message) == (sent.code, sent.message)


def test_a_changed_transcript_still_asks_the_reader_to_reload() -> None:
    received = round_trip(ReadRefused(10, TranscriptChanged(), "trace"), ReadRefused).error
    assert type(received) is TranscriptChanged and isinstance(received, ReloadRequired)
    assert str(received) == str(TranscriptChanged())


def test_a_changed_draft_keeps_retry_semantics_across_the_observation_wire() -> None:
    sent = StaleRevision("Transcript changed while reading its current draft")
    received = round_trip(ReadRefused(11, sent, "service trace"), ReadRefused).error
    assert type(received) is StaleRevision
    assert isinstance(received, ReadAgain)
    assert isinstance(received, DeferredRead)
    assert received.args == sent.args
