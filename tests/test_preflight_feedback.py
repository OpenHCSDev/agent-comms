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
    assert output.failure_text == 'Original preflight phase and timing\nBackend startup reported:\nNative startup cause'
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


def test_post_dispatch_failure_preserves_bound_uncertainty(tmp_path):
    from agent_comms.acp_failure import ACPFailure
    from agent_comms.input_attempt import BoundUnknownInput, SentInput
    from agent_comms.input_disposition import InputDispositions

    store = InputDispositions(tmp_path / InputDispositions.filename)
    store.record('acp:partial-write', seq=None, owner='owner', admission=1,
                 target='owner', text='original input')
    assert store.bind('acp:partial-write', admission=1, turn_id='turn',
                      native_id='a' * 32, text='original input')
    before = store.read().lookup('acp:partial-write')
    assert isinstance(before, SentInput)
    assert isinstance(before, BoundUnknownInput)
    output = TurnOutput(failure=InputIdUnavailable('Native prompt write failed after dispatch'))
    output.startup_error('BrokenPipeError: pipe closed during write')
    assert 'not sent' not in output.failure_text.lower()
    assert 'BrokenPipeError' in output.failure_text
    after = store.settle_unbound(('acp:partial-write',))
    assert after.lookup('acp:partial-write') == before
    from dataclasses import replace
    feedback = replace(ACPFailure.from_error(-32603, output.failure_text),
                       input_state=after.shared_state(('acp:partial-write',)))
    assert feedback.input_state is BoundUnknownInput
    assert 'Unknown' in feedback.feedback
    assert 'not sent' not in feedback.feedback.lower()
