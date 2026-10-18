"""Real command defaults retain the wire capabilities of their declarations."""
import json

import pytest

from agent_comms.cli_commands import ForkCliCommand
from agent_comms.field_codec import FieldCodec, compile_decoder
from agent_comms.pi_vocabulary import HighThinkingLevel, NoThinkingOverride, ThinkingLevelField
from agent_comms.read_basis import OnReadWatchAcknowledgment, WatchAcknowledgment
from agent_comms.thread_execution import ExternalThreadExecution, ThreadExecution


@pytest.mark.parametrize('annotation,value,encoded', (
    (type[WatchAcknowledgment], OnReadWatchAcknowledgment, OnReadWatchAcknowledgment.declared_name),
    (type[ThreadExecution], ExternalThreadExecution, ExternalThreadExecution.declared_name),
    (ThinkingLevelField, NoThinkingOverride, None),
    (ThinkingLevelField, HighThinkingLevel, 'high'),
    (type(None), None, None),
    (str | None, None, None),
    (bool, True, True),
    (tuple[int, ...], (1, 2), [1, 2]),
))
def test_declared_default_editor_round_trip(annotation, value, encoded):
    decoder = compile_decoder(annotation)
    assert decoder.target == annotation
    text = decoder.encode_editor(value)
    assert text == json.dumps(encoded)
    assert FieldCodec.decode(annotation, decoder.decode_editor(text)) == value


def test_fork_default_retains_annotated_thinking_representation(tmp_path):
    command = ForkCliCommand(name='', parent='saved', project=str(tmp_path),
                             model=None, thinking_level=NoThinkingOverride)
    editable = {entry.name: entry for entry in command.editable_fields}
    assert editable['thinking_level'].annotation == ThinkingLevelField
    assert editable['thinking_level'].editor_default == 'null'
    payload = FieldCodec.encode(command)
    payload.update(command.editor_arguments({'thinking_level': '"high"'}))
    assert FieldCodec.decode(ForkCliCommand, payload).thinking_level is HighThinkingLevel
    assert compile_decoder(str).encode_editor('literal text') == 'literal text'


@pytest.mark.asyncio
async def test_unconfigured_catalog_default_is_an_actual_editor_choice(monkeypatch, tmp_path):
    from acp.schema import SessionConfigSelectOption
    from agent_comms.config_options import ConfigOptions, ThinkingLevelConfigOption

    async def discover(self, selection):
        return [SessionConfigSelectOption(name='High', value='high')]

    monkeypatch.setattr(ThinkingLevelConfigOption, 'discover', discover)
    command = ForkCliCommand(name='', parent='saved', project=str(tmp_path),
                             model=None, thinking_level=NoThinkingOverride)
    field = next(item for item in command.editable_fields if item.name == 'thinking_level')
    prepared = await field.prepare(ConfigOptions.configured(), command)
    assert ('Not configured', prepared.editor_default) in prepared.choices
    assert prepared.editor_default == 'null'
    assert ('High', '"high"') in prepared.choices
    assert command.editor_arguments({'thinking_level': prepared.editor_default}) == {'thinking_level': None}
