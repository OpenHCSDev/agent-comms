"""Input decoding supplies behavior-bearing selections to command execution."""

import pytest

from agent_comms.cli_commands import ForkCliCommand, OneTarget, PinThreadCliCommand, TargetBatch
from agent_comms.channel_targets import CommandTarget


@pytest.mark.parametrize(('declaration', 'raw', 'kind', 'names'), [
    (ForkCliCommand, CommandTarget.named('alpha'), OneTarget, ('alpha',)),
    (ForkCliCommand, (CommandTarget.named('alpha'),), OneTarget, ('alpha',)),
    (ForkCliCommand, (CommandTarget.named('alpha'), CommandTarget.named('alpha')), OneTarget, ('alpha',)),
    (ForkCliCommand, (CommandTarget.named('alpha'), CommandTarget.named('beta')), TargetBatch, ('alpha', 'beta')),
    (PinThreadCliCommand, CommandTarget.named('alpha'), OneTarget, ('alpha',)),
    (PinThreadCliCommand, (CommandTarget.named('alpha'),), TargetBatch, ('alpha',)),
])
def test_single_element_batch_rule_is_decoded_once(declaration, raw, kind, names):
    selected = declaration.selected_targets(raw)
    assert type(selected) is kind
    assert tuple(target.name for target in selected.targets) == names


def test_empty_target_input_is_refused():
    with pytest.raises(ValueError):
        ForkCliCommand.selected_targets(())
