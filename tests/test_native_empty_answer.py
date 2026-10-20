"""Empty completions fail the request without declaring a dead native runtime."""
import pytest

from agent_comms.native_pi import NativeEmptyAnswer, NativePiUnavailable, NativeRequestFailure
from agent_comms.tracked_turn import (
    CompletedTrackedTerminal,
    EmptyAnswerTrackedTerminal,
    PendingTrackedTerminal,
    UnansweredNotifiedTrackedTerminal,
)


def test_empty_final_and_notifications_keep_the_model_outcome():
    terminal = PendingTrackedTerminal().append("")
    assert isinstance(terminal, EmptyAnswerTrackedTerminal)
    for _ in range(6):
        terminal = terminal.notification([]).append("")
        assert isinstance(terminal, EmptyAnswerTrackedTerminal)
    with pytest.raises(NativeEmptyAnswer) as caught:
        terminal.require_response([])
    assert isinstance(caught.value, NativeRequestFailure)
    assert not isinstance(caught.value, NativePiUnavailable)
    completed = terminal.append("Actual answer")
    assert isinstance(completed, CompletedTrackedTerminal)
    assert completed.require_response(["Actual answer"]) == "Actual answer"


def test_empty_final_after_unanswered_notice_is_not_missing_response():
    terminal = UnansweredNotifiedTrackedTerminal().append("")
    assert isinstance(terminal, EmptyAnswerTrackedTerminal)
    with pytest.raises(NativeEmptyAnswer):
        terminal.notification([]).require_response([])
