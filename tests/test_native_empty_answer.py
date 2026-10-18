"""Empty completions fail the request without declaring a dead native runtime."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_comms.backend import TurnSession
from agent_comms.native_pi import NativeEmptyAnswer, NativePiUnavailable, NativeRequestFailure
from agent_comms.selected_request import SelectedRequest
from agent_comms.tracked_turn import (
    CompletedTrackedTerminal,
    EmptyAnswerTrackedTerminal,
    PendingTrackedTerminal,
    TrackedTurnSession,
    UnansweredNotifiedTrackedTerminal,
)


def test_empty_final_and_notifications_keep_the_model_outcome():
    terminal = PendingTrackedTerminal().append("")
    assert isinstance(terminal, EmptyAnswerTrackedTerminal)
    for _ in range(6):
        terminal = terminal.notification([]).append("")
        assert isinstance(terminal, EmptyAnswerTrackedTerminal)
    with pytest.raises(NativeEmptyAnswer, match="^The model finished with no text$") as caught:
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


def test_model_empty_does_not_enter_acquisition_unavailable_recovery():
    fail = Mock()
    turn = SimpleNamespace(prompt_state=SimpleNamespace(raise_native_failure=fail))
    with pytest.raises(NativeEmptyAnswer), TurnSession.native_acquisition(turn):
        raise NativeEmptyAnswer()
    fail.assert_not_called()


async def test_proved_empty_completion_can_retain_the_healthy_child():
    turn = SimpleNamespace(
        startup=SimpleNamespace(measurements=SimpleNamespace(operation=lambda _: nullcontext())),
        context_proof=AsyncMock(return_value=object()),
        output=SimpleNamespace(raise_if_failed=Mock()), model=object(),
        tool_socket=SimpleNamespace(assert_complete=Mock(), retainable=True),
        terminal=EmptyAnswerTrackedTerminal(), text_parts=[], retainable=False,
    )
    with pytest.raises(NativeEmptyAnswer):
        await TrackedTurnSession.result(turn)
    assert turn.retainable
    turn.context_proof.assert_awaited_once()
    turn.tool_socket.assert_complete.assert_called_once()


async def test_selected_request_publishes_model_empty_without_unavailable_label(monkeypatch):
    import agent_comms.selected_request as selected

    participant = object()
    request = SelectedRequest(participant, SimpleNamespace(input_id="e" * 32))
    settle = AsyncMock(return_value=True)
    publish = Mock()
    monkeypatch.setattr(SelectedRequest, "_settle_unknown", settle)
    monkeypatch.setattr(selected, "publish_native_failure", publish)

    async def run_worker(operation):
        return operation()

    monkeypatch.setattr(selected.Coordination, "run_worker", run_worker)
    error = NativeEmptyAnswer()
    with pytest.raises(NativeEmptyAnswer):
        async with request.native_failures():
            raise error
    settle.assert_awaited_once()
    publish.assert_called_once_with(
        participant, "e" * 32, "The model finished with no text",
        native_response=None, source_error=error,
    )
