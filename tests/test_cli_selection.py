"""Input decoding supplies behavior-bearing selections to command execution."""

import pytest

from agent_comms.channel_targets import ChannelSelection, NoChannel, OneChannel, PerTargetChannels
from agent_comms.cli_commands import ForkCliCommand, OneTarget, PinThreadCliCommand, TargetBatch


@pytest.mark.parametrize(('raw', 'selected', 'channels'), [
    (None, NoChannel(), (None,)),
    ('#team', OneChannel('#team'), ('#team',)),
    ({'alpha': ('#team', '#other')}, PerTargetChannels({'alpha': ('#team', '#other')}), ('#team', '#other')),
])
def test_channel_input_decodes_to_its_behavior(raw, selected, channels):
    decoded = ChannelSelection.decode(raw)
    assert decoded == selected
    assert decoded.channels_for('alpha') == channels
    if isinstance(decoded, PerTargetChannels):
        assert decoded.channels_for('missing') == (None,)
        with pytest.raises(ValueError, match='does not accept per-target channels'):
            decoded.require_unbound()
    else:
        decoded.require_unbound()


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
