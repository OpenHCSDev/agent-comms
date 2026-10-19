"""Input decoding supplies behavior-bearing selections to command execution."""

import pytest

from agent_comms.cli_commands import ForkCliCommand, OneTarget, PinThreadCliCommand, TargetBatch


@pytest.mark.parametrize(('declaration', 'raw', 'kind'), [
    (ForkCliCommand, 'alpha', OneTarget),
    (ForkCliCommand, ('alpha',), OneTarget),
    (ForkCliCommand, ('alpha', 'alpha'), OneTarget),
    (ForkCliCommand, ('alpha', 'beta'), TargetBatch),
    (PinThreadCliCommand, 'alpha', OneTarget),
    (PinThreadCliCommand, ('alpha',), TargetBatch),
])
def test_single_element_batch_rule_is_decoded_once(declaration, raw, kind):
    selected = declaration.selected_targets(raw)
    assert type(selected) is kind
    assert tuple(target.name for target in selected.targets) == tuple(dict.fromkeys(
        (raw,) if isinstance(raw, str) else raw))


def test_empty_target_input_is_refused():
    with pytest.raises(ValueError, match='Select at least one target'):
        ForkCliCommand.selected_targets(())
