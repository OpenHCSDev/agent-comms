"""Empty completions fail the request without declaring a dead native runtime."""
import pytest

from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_pi import NativeEmptyAnswer
from agent_comms.execution.tracked_turn import (
    PendingTrackedTerminal,
    UnansweredNotifiedTrackedTerminal,
)


def test_empty_final_and_notifications_keep_the_model_outcome():
    terminal = PendingTrackedTerminal().append("")
    for _ in range(6):
        terminal = terminal.notification([]).append("")
    with pytest.raises(NativeEmptyAnswer) as caught:
        terminal.require_response([])
    assert not isinstance(caught.value, NativePiUnavailable)
    assert terminal.append("Actual answer").require_response(["Actual answer"]) == "Actual answer"


def test_empty_final_after_unanswered_notice_is_not_missing_response():
    terminal = UnansweredNotifiedTrackedTerminal().append("")
    with pytest.raises(NativeEmptyAnswer):
        terminal.notification([]).require_response([])
