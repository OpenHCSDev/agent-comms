"""Real command defaults retain the wire capabilities of their declarations."""
import json

import pytest

from agent_comms.cli_commands import ForkCliCommand
from agent_comms.field_codec import FieldCodec, compile_decoder
from agent_comms.pi_vocabulary import HighThinkingLevel, NoThinkingOverride, ThinkingLevelField
from agent_comms.read_basis import OnReadWatchAcknowledgment, WatchAcknowledgment
from agent_comms.thread_execution import ExternalThreadExecution, ThreadExecution
from agent_comms.thread_model import UndeclaredModel


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
                             model=UndeclaredModel(), thinking_level=NoThinkingOverride)
    editable = {entry.name: entry for entry in command.editable_fields}
    decoder = compile_decoder(editable['thinking_level'].annotation)
    assert decoder.encode_editor(HighThinkingLevel) == '"high"'
    assert editable['thinking_level'].editor_default == 'null'
    payload = FieldCodec.encode(command)
    payload.update(command.editor_arguments({'thinking_level': '"high"'}))
    assert FieldCodec.decode(ForkCliCommand, payload).thinking_level is HighThinkingLevel
    assert compile_decoder(str).encode_editor('literal text') == 'literal text'


