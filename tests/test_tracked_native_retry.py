import pytest

from agent_comms.native_arguments import SelectedModel

from agent_comms.agent_backend import WhenBusy
from agent_comms.pi_events import NativeRetryEnd
from agent_comms.pi_payloads import FailedProviderRetry, PiMessage, SucceededProviderRetry
from agent_comms.tracked_turn import CompletedTrackedTerminal, PendingTrackedTerminal, TrackedTurnSession
from agent_comms.turn_input_source import AcceptedFollowingInput, DeliveredFollowingInput
from agent_comms.turn_goal_permission import InactiveGoalPermission
from agent_comms.native_pi import NativeEmptyAnswer, NativePiTerminalFailure, NativePiUnavailable
from agent_comms.turn_output import TurnOutput
from agent_comms.turn_failure import ModelRequestFailed, RetryableProviderFailure


async def test_recovered_native_request_replaces_prior_refusal_terminal():
    turn = object.__new__(TrackedTurnSession)
    turn.output = TurnOutput(failure=ModelRequestFailed("provider refusal"))
    turn.terminal = PendingTrackedTerminal()

    event = NativeRetryEnd.from_wire({
        "type": "native_retry_end", "success": True, "attempt": 1
    })
    assert isinstance(event.outcome, SucceededProviderRetry)
    assert event.to_wire() == {
        "type": "native_retry_end", "success": True, "attempt": 1
    }
    await turn.native_retry_finished(event)

    assert turn.output.failure is None
    assert turn.output.retry_result is None
    assert isinstance(turn.terminal, PendingTrackedTerminal)


@pytest.mark.parametrize("previous", [None, ModelRequestFailed("prior refusal")])
async def test_exhausted_native_request_records_required_final_error(previous):
    turn = object.__new__(TrackedTurnSession)
    turn.output = TurnOutput(failure=previous)
    turn.terminal = PendingTrackedTerminal()
    event = NativeRetryEnd.from_wire({
        "type": "native_retry_end",
        "success": False,
        "attempt": 3,
        "finalError": "final refusal",
    })

    assert isinstance(event.outcome, FailedProviderRetry)
    assert event.to_wire() == {
        "type": "native_retry_end",
        "success": False,
        "attempt": 3,
        "finalError": "final refusal",
    }
    await turn.native_retry_finished(event)

    assert turn.output.declared_failure == ModelRequestFailed("final refusal")
    with pytest.raises(NativePiTerminalFailure, match="final refusal") as caught:
        turn.output.raise_if_failed(None, SelectedModel("configured", "model"))
    assert not isinstance(caught.value.failure, RetryableProviderFailure)
    PiMessage.from_wire({
        "role": "assistant", "content": [], "stopReason": "error",
        "errorMessage": "final refusal",
    }).tracked_end(turn)
    assert turn.output.declared_failure == ModelRequestFailed("final refusal")


def test_failed_native_retry_end_requires_final_error_at_decode():
    with pytest.raises(ValueError, match="requires finalError"):
        NativeRetryEnd.from_wire({
            "type": "native_retry_end", "success": False, "attempt": 3
        })


def following(kind):
    values = dict(keys=(), accepted_id="fresh-following", goal_permission=InactiveGoalPermission())
    return kind(**values, origins=()) if kind is DeliveredFollowingInput else kind(**values)


@pytest.mark.parametrize("text", ["", '{"decision":"IGNORE"}', "Background answer"])
def test_delivered_notice_preserves_original_but_real_steering_can_revise(text):
    notice = following(DeliveredFollowingInput)
    steering = following(AcceptedFollowingInput)
    assert notice.when_busy is WhenBusy.STEER
    assert steering.when_busy is WhenBusy.STEER
    response = notice.continue_response(CompletedTrackedTerminal("Requested answer"), ["Requested answer"])
    response = response.tool_round().append(text)
    assert response.require_response([text]) == "Requested answer"
    response = notice.continue_response(response, [text]).append("Another background answer")
    assert response.require_response(["Another background answer"]) == "Requested answer"
    response = steering.continue_response(response, []).append("Revised requested answer")
    assert response.require_response(["Revised requested answer"]) == "Revised requested answer"


@pytest.mark.parametrize("response,parts", [
    (CompletedTrackedTerminal("Unmatched original"), ["Different streamed text"]),
])
@pytest.mark.parametrize("text", ["", '{"decision":"IGNORE"}', "Background answer"])
def test_notice_cannot_supply_an_unproved_original_answer(response, parts, text):
    response = following(DeliveredFollowingInput).continue_response(response, parts)
    response = response.tool_round().append(text)
    failure = NativeEmptyAnswer if not text else NativePiUnavailable
    with pytest.raises(failure):
        response.require_response([text])
    response = following(AcceptedFollowingInput).continue_response(response, [text])
    response = response.append("Real user requested this answer")
    assert response.require_response(["Real user requested this answer"]) == "Real user requested this answer"


def test_midtool_delivered_message_does_not_erase_pending_selected_answer():
    # Pi starts the steered DM after the bash result and before the only final.
    # The original selected message and the DM share this completed response;
    # the response publisher already links a started same-route DM once.
    pending = PendingTrackedTerminal().tool_round()
    pending = following(DeliveredFollowingInput).continue_response(pending, [])
    with pytest.raises(NativePiUnavailable, match="no unique authoritative completed response"):
        pending.require_response([])
    completed = pending.append("Shared final answer")
    assert completed.require_response(["Shared final answer"]) == "Shared final answer"


async def test_successful_provider_recovery_does_not_discard_retained_requested_answer():
    turn = object.__new__(TrackedTurnSession)
    turn.output = TurnOutput(failure=ModelRequestFailed("503 overloaded"))
    turn.terminal = following(DeliveredFollowingInput).continue_response(
        CompletedTrackedTerminal("Requested answer"), ["Requested answer"])
    await turn.native_retry_finished(NativeRetryEnd.from_wire({
        "type": "native_retry_end", "success": True, "attempt": 1,
    }))
    assert turn.output.failure is None
    turn.terminal = turn.terminal.append("")
    assert turn.terminal.require_response([]) == "Requested answer"
