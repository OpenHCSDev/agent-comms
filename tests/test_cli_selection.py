"""Input decoding supplies behavior-bearing selections to command execution."""

import pytest

from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.cli_command import OneTarget, TargetBatch
from agent_comms.cli_verbs.pin_thread import PinThreadCliCommand
from agent_comms.channel_targets import CommandTarget


@pytest.mark.parametrize(('declaration', 'raw', 'kind', 'names'), [
    (ForkCliCommand, (CommandTarget.named('alpha'),), OneTarget, ('alpha',)),
    (ForkCliCommand, (CommandTarget.named('alpha'), CommandTarget.named('alpha')), OneTarget, ('alpha',)),
    (ForkCliCommand, (CommandTarget.named('alpha'), CommandTarget.named('beta')), TargetBatch, ('alpha', 'beta')),
    (PinThreadCliCommand, (CommandTarget.named('alpha'),), TargetBatch, ('alpha',)),
])
def test_the_declaration_owns_the_selection_kind(declaration, raw, kind, names):
    selected = declaration.selected_targets(raw)
    assert type(selected) is kind
    assert tuple(target.name for target in selected.targets) == names


def test_empty_target_input_is_refused():
    with pytest.raises(ValueError):
        ForkCliCommand.selected_targets(())


def test_one_cli_name_for_a_batch_command_is_a_batch():
    from agent_comms.cli_verbs.start import StartCliCommand
    from agent_comms.cli_verbs.stop import StopCliCommand

    for declaration in (StartCliCommand, StopCliCommand):
        selected = declaration.selected_targets(CommandTarget.arguments(('alpha',), '/p'))
        assert type(selected) is TargetBatch
