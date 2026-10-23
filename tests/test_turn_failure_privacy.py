"""Failure storage owns privacy without changing the declared failure's behavior."""

import pytest

from agent_comms.turn_failure import (
    InputIdUnavailable,
    ModelRequestFailed,
    ProviderRetryExhausted,
)
from agent_comms.turn_output import TurnOutput


@pytest.mark.parametrize('kind', [InputIdUnavailable, ModelRequestFailed, ProviderRetryExhausted])
@pytest.mark.parametrize('sensitive', [False, True])
def test_recorded_failure_has_one_safe_text_and_keeps_its_behavior(kind, sensitive):
    original = kind('private provider response body')
    output = TurnOutput(sensitive=sensitive)
    output.record_failure(original)
    stored = output.declared_failure
    expected = output.redact(original.text)
    assert stored is not original and type(stored) is kind
    assert original.text == 'private provider response body'
    assert stored.text == output.failure_text == expected
    assert output.terminal_text(False, 'private stderr', 1) == expected
    assert stored.code == original.code
    assert stored.declared_position() == original.declared_position()
    assert stored.ends_with_request_retry == original.ends_with_request_retry
    if sensitive:
        assert original.text not in repr(output)


def test_redacted_copy_keeps_failure_precedence_and_inherited_copy_operation():
    output = TurnOutput(sensitive=True)
    output.record_failure(InputIdUnavailable('private startup failure'))
    selected = output.declared_failure
    output.record_failure(ModelRequestFailed('another private failure'))
    assert output.declared_failure is selected
    output.startup_error('private native stderr')
    assert output.declared_failure is selected
    changed = selected.with_startup_diagnostics('ordinary startup evidence')
    assert type(changed) is InputIdUnavailable
    assert changed.text == selected.text + '\nBackend startup reported:\nordinary startup evidence'


def test_later_failure_of_the_same_kind_keeps_the_first_text():
    output = TurnOutput()
    output.record_failure(ModelRequestFailed('first provider failure'))
    first = output.declared_failure
    output.record_failure(ModelRequestFailed('later provider failure'))
    assert output.declared_failure is first
    assert output.failure_text == 'first provider failure'
