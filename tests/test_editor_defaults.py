"""Real command defaults retain the wire capabilities of their declarations."""
import json

import pytest

from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.field_codec import FieldCodec, MalformedRecord, compile_decoder
from agent_comms.target_fields import TargetField
from agent_comms.thread_management import ChangedTags, GivenTags, InheritedTags, TagChoice
from agent_comms.pi_vocabulary import HighThinkingLevel, NoThinkingOverride, ThinkingLevelField
from agent_comms.repo import LaunchCheckout
from agent_comms.thread_execution import ExternalThreadExecution, ThreadExecution
from agent_comms.thread_model import UndeclaredModel


@pytest.mark.parametrize('annotation,value,encoded', (
    (type[ThreadExecution], ExternalThreadExecution, ExternalThreadExecution.declared_name),
    (ThinkingLevelField, NoThinkingOverride, None),
    (ThinkingLevelField, HighThinkingLevel, 'high'),
    (type(None), None, None),
    (str | None, None, None),
    (bool, True, True),
    (tuple[int, ...], (1, 2), [1, 2]),
    (TagChoice, GivenTags(frozenset({'auth'})), 'auth'),
    (TagChoice, InheritedTags(), None),
    (TagChoice, ChangedTags(frozenset({'auth'}), frozenset({'old'})),
     {'add': ['auth'], 'remove': ['old']}),
))
def test_declared_default_editor_round_trip(annotation, value, encoded):
    decoder = compile_decoder(annotation)
    assert decoder.target == annotation
    text = decoder.encode_editor(value)
    assert text == json.dumps(encoded)
    assert FieldCodec.decode(annotation, decoder.decode_editor(text)) == value


def test_fork_default_retains_annotated_thinking_representation(source_repository):
    command = ForkCliCommand(name='', parent='saved', project=LaunchCheckout.checked(source_repository.checkout),
                             model=UndeclaredModel(), thinking_level=NoThinkingOverride)
    editable = {entry.name: entry for entry in command.editable_fields}
    decoder = compile_decoder(editable['thinking_level'].annotation)
    assert decoder.encode_editor(HighThinkingLevel) == '"high"'
    assert editable['thinking_level'].editor_default == 'null'
    payload = FieldCodec.encode(command)
    payload.update(command.editor_arguments({'thinking_level': '"high"'}))
    assert FieldCodec.decode(ForkCliCommand, payload).thinking_level is HighThinkingLevel
    assert compile_decoder(str).encode_editor('literal text') == 'literal text'


@pytest.mark.parametrize('annotation,value', (
    (TagChoice, GivenTags(frozenset())),
    (TagChoice, GivenTags(frozenset({'auth'}))),
    (TagChoice, InheritedTags()),
    (TagChoice, ChangedTags(frozenset({'auth'}), frozenset({'old'}))),
    (int | float, 3),
    (int | float, 3.0),
    (int | float | None, None),
))
def test_native_union_member_and_field_derived_transaction(annotation: object, value: object) -> None:
    decoder = compile_decoder(annotation)
    assert decoder.editor_initial(value) is value
    member = decoder.editor_wire.selected_editor_member(value)
    assert member.declaration is type(value)
    assert tuple(item.declaration for item in decoder.editor_wire.editor_members).count(type(value)) == 1
    parameters = tuple(TargetField.of(entry, member.current(entry, value)) for entry in member.fields)
    arguments = {key: item for parameter in parameters
                 for key, item in parameter.argument(parameter.editor_initial).items()}
    result = member.complete(arguments)
    assert type(result) is type(value)
    assert result == value
    json.dumps(decoder.encode_editor(value))
    assert decoder.decode_editor(decoder.editor_wire.editor_argument(value)) == FieldCodec.encode(value, annotation)


def test_overlapping_scalar_wire_does_not_change_selected_native_member() -> None:
    decoder = compile_decoder(int | float)
    member = decoder.editor_wire.selected_editor_member(3.0)
    assert member.declaration is float
    assert member.complete({'value': '3.0'}) == 3.0
    with pytest.raises(MalformedRecord):
        member.complete({'value': '3'})
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(int | float, 3)


def test_native_union_default_does_not_leak_into_public_json_schema() -> None:
    plan = next(entry for entry in FieldCodec.argument_fields(ForkCliCommand) if entry.name == 'tags')
    value = ChangedTags(frozenset({'auth'}), frozenset())
    parameter = TargetField.of(plan, value)
    assert parameter.editor_initial is value
    assert parameter.editor_selection(value) is ChangedTags
    assert isinstance(parameter.editor_default, str)
    json.dumps(parameter.json_schema())


def test_record_member_inputs_remain_required_without_invented_values() -> None:
    decoder = compile_decoder(TagChoice)
    member = decoder.editor_wire.selected_editor_member(ChangedTags(frozenset(), frozenset()))
    assert all(entry.constructor_required for entry in member.fields)
    with pytest.raises(MalformedRecord):
        member.complete({})


