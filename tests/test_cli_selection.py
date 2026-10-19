"""Input decoding supplies behavior-bearing selections to command execution."""

import pytest

from agent_comms.cli_commands import ForkCliCommand, OneTarget, PinThreadCliCommand, TargetBatch


@pytest.mark.parametrize(('declaration', 'raw', 'kind', 'names'), [
    (ForkCliCommand, 'alpha', OneTarget, ('alpha',)),
    (ForkCliCommand, ('alpha',), OneTarget, ('alpha',)),
    (ForkCliCommand, ('alpha', 'alpha'), OneTarget, ('alpha',)),
    (ForkCliCommand, ('alpha', 'beta'), TargetBatch, ('alpha', 'beta')),
    (PinThreadCliCommand, 'alpha', OneTarget, ('alpha',)),
    (PinThreadCliCommand, ('alpha',), TargetBatch, ('alpha',)),
])
def test_single_element_batch_rule_is_decoded_once(declaration, raw, kind, names):
    selected = declaration.selected_targets(raw)
    assert type(selected) is kind
    assert tuple(target.name for target in selected.targets) == names


def test_empty_target_input_is_refused():
    with pytest.raises(ValueError, match='Select at least one target'):
        ForkCliCommand.selected_targets(())
