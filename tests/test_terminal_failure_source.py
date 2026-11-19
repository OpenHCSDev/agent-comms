"""A new failed assistant carries its original evidence; no request is executed."""
import json
from pathlib import Path

import pytest

from agent_comms.execution.diagnostics import record_terminal_failure
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.pi_events import PiEvent
from agent_comms.execution.turn_failure import ModelRequestFailed, NativePiTerminalFailure
from agent_comms.execution.turn_output import TurnOutput
from agent_comms.nominal.field_codec import TurnId

FIXTURE = Path(__file__).with_name('fixtures') / 'native_provider_transport_1011.json'


def failed_event(unknown=False):
    record = json.loads(FIXTURE.read_text())
    if unknown:
        record['message']['diagnostics'] = [dict(type='future_transport', payload={'original': [1,2,3]})]
    else:
        record['message']['diagnostics'][0]['error']['stack'] = 'Error: original transport\n at socketClose (native:42)'
    return PiEvent.from_wire(record)


def terminal(output):
    with pytest.raises(NativePiTerminalFailure) as caught:
        output.raise_if_failed(None, SelectedModel('provider', 'model'))
    return caught.value


@pytest.mark.parametrize('unknown', [False, True])
def test_private_failure_keeps_original_source_and_stderr(tmp_path, unknown):
    event = failed_event(unknown)
    output = TurnOutput()
    output.record_native_failure(event.message)
    output.record_failure(ModelRequestFailed('original provider error'))
    error = terminal(output)
    error.attach_stderr('original child stderr')
    assert error.native_source.message is event.message
    path = record_terminal_failure(tmp_path, subject=TurnId.mint(), thread='fixture',
                                   failure=error.failure, sequences=(), source_error=error)
    saved = json.loads(path.read_text())
    assert saved['native']['diagnostics'] == [d.to_wire() for d in event.message.diagnostics]
    assert saved['native']['stderr'] == 'original child stderr'
    if not unknown:
        assert saved['native']['diagnostics'][0]['error']['stack'].startswith('Error: original transport')
    assert error.public_message == 'provider/model: request failed.'
    assert 'must not be replayed' in saved['outcome']
    assert path.stat().st_mode & 0o777 == 0o600


def test_sensitive_failure_uses_existing_source_redaction(tmp_path):
    event = failed_event()
    output = TurnOutput(sensitive=True)
    output.record_native_failure(event.message)
    output.record_failure(ModelRequestFailed('SECRET response body'))
    error = terminal(output)
    assert error.native_source.message.parts == ()
    assert error.native_source.message.diagnostics == ()
    path = record_terminal_failure(tmp_path, subject=TurnId.mint(), thread='fixture',
                                   failure=error.failure, sequences=(), source_error=error)
    raw = path.read_text()
    assert json.loads(raw)['native'] == {'stderr': '', 'diagnostics': []}
    assert 'SECRET' not in raw
    assert 'socketClose' not in raw


def test_absent_source_and_retried_request_do_not_borrow_old_evidence():
    output = TurnOutput()
    output.record_native_failure(failed_event().message)
    output.record_failure(ModelRequestFailed('earlier request failed'))
    output.request_retried()
    output.record_failure(ModelRequestFailed('later request has no native message'))
    assert terminal(output).diagnostic_evidence() == {'stderr': ''}
    fresh = TurnOutput()
    fresh.record_failure(ModelRequestFailed('local failure'))
    assert terminal(fresh).diagnostic_evidence() == {'stderr': ''}
