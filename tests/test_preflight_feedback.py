"""Failure diagnostics and durable delivery projection remain independent authorities."""
from agent_comms.field_codec import TurnId
import pytest
from agent_comms.input_attempt import ReservedInput, NotSentInput, MissingInput
from agent_comms.input_disposition import InputDocument
from agent_comms.turn_failure import InputIdUnavailable, NativePreflightExit, NativePreflightTimeout, PromptSendFailed
from agent_comms.turn_output import TurnOutput
from stored_inputs import stored_inputs

@pytest.mark.parametrize('kind', [NativePreflightExit, NativePreflightTimeout])
def test_preflight_diagnostics_keep_original_phase(kind):
    output = TurnOutput()
    output.record_failure(kind('Original preflight phase and timing'))
    output.startup_error('Native startup cause\n')
    assert output.failure_text == 'Original preflight phase and timing\nBackend startup reported:\nNative startup cause'
    output = TurnOutput(sensitive=True)
    output.record_failure(kind('Private phase'))
    output.startup_error('private image bytes')
    assert output.failure_text == 'Image prompt failed; backend diagnostics withheld.'
    assert type(output.failure) is kind
    assert 'Private phase' not in repr(output)
    assert 'private image bytes' not in repr(output)
    ordinary = TurnOutput(failure=PromptSendFailed('Transport cause'))
    ordinary.startup_error('unrelated startup text')
    assert ordinary.failure_text == 'Transport cause'

def test_delivery_projection_requires_complete_homogeneous_actual_rows():
    reserved = ReservedInput('a', None, 'owner', 1, 'owner', 'input')
    unsent = reserved.finish_unbound().require_moved('Reserved input stayed reserved').state
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

    store = InputDispositions(tmp_path)
    store.record('acp:partial-write', seq=None, owner='owner', claim_scope=1,
                 target='owner', text='original input')
    assert store.bind('acp:partial-write', claim_scope=1, turn_id=TurnId('9b473845f684444f80a26586ae298d2b'),
                      native_id='a' * 32, text='original input')
    before = stored_inputs(store.root).lookup('acp:partial-write')
    assert isinstance(before, SentInput)
    assert isinstance(before, BoundUnknownInput)
    output = TurnOutput(failure=InputIdUnavailable('Native prompt write failed after dispatch'))
    output.startup_error('BrokenPipeError: pipe closed during write')
    assert 'not sent' not in output.failure_text.lower()
    assert 'BrokenPipeError' in output.failure_text
    after = store.settle_unbound(('acp:partial-write',))
    assert after.lookup('acp:partial-write') == before
    from dataclasses import replace
    feedback = replace(ACPFailure.from_detail(-32603, output.failure_text),
                       input_state=after.shared_state(('acp:partial-write',)))
    assert feedback.input_state is BoundUnknownInput
    assert 'Unknown' in feedback.feedback
    assert 'not sent' not in feedback.feedback.lower()
