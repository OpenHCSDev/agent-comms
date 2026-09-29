"""Failure diagnostics and durable delivery projection remain independent authorities."""
import pytest
from agent_comms.input_attempt import ReservedInput, NotSentInput, MissingInput
from agent_comms.input_disposition import InputDocument
from agent_comms.turn_failure import InputIdUnavailable, PromptSendFailed
from agent_comms.turn_output import TurnOutput
from agent_comms.diagnostics import FailureReason

@pytest.mark.parametrize('reason', [FailureReason.PREFLIGHT_EXIT, FailureReason.PREFLIGHT_TIMEOUT])
def test_preflight_diagnostics_keep_original_phase(reason):
    output = TurnOutput(preflight_failure=reason)
    output.record_failure(InputIdUnavailable('Original preflight phase and timing'))
    output.startup_error('Native startup cause\n')
    assert output.failure_text == 'Original preflight phase and timing\nThe prompt was not sent. Backend startup reported:\nNative startup cause'
    output = TurnOutput(sensitive=True, preflight_failure=reason)
    output.record_failure(InputIdUnavailable('Private phase'))
    output.startup_error('private image bytes')
    assert output.failure_text == 'Private phase'
    ordinary = TurnOutput(failure=PromptSendFailed('Transport cause'))
    ordinary.startup_error('unrelated startup text')
    assert ordinary.failure_text == 'Transport cause'

def test_delivery_projection_requires_complete_homogeneous_actual_rows():
    reserved = ReservedInput('a', None, 'owner', 1, 'owner', 'input')
    unsent = reserved.finish_unbound()
    assert isinstance(unsent, NotSentInput)
    document = InputDocument(rows={'a': unsent})
    assert document.shared_state(('a',)) is NotSentInput
    assert document.shared_state(('a', 'missing')) is None
    assert document.shared_state(()) is None
    assert document.shared_state(('missing',)) is None
