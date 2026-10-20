"""Recursive wire declarations share one definition in their schema document."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Annotated

import pytest

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, MalformedRecord, WireForm
from agent_comms.todos import ProblemOrder


@dataclass(frozen=True)
class RecursiveReply:
    text: str
    children: tuple[RecursiveReply, ...] = ()


class RecursiveChoice(DeclaredFamily, affix='RecursiveChoice'):
    pass


@dataclass(frozen=True)
class TextRecursiveChoice(RecursiveChoice):
    text: str


@dataclass(frozen=True)
class NestedRecursiveChoice(RecursiveChoice):
    children: tuple[RecursiveChoice, ...]


def references(schema):
    if isinstance(schema, dict):
        if '$ref' in schema:
            yield schema['$ref']
        for value in schema.values():
            yield from references(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from references(value)


def test_recursive_record_schema_has_one_definition_and_resolves_every_reference():
    schema = FieldCodec.value_schema(RecursiveReply)
    name = f'{RecursiveReply.__module__}.{RecursiveReply.__qualname__}'
    assert set(schema['$defs']) == {name}
    assert schema['properties']['children']['items'] == {'$ref': f'#/$defs/{name}'}
    assert schema['$defs'][name] == {key: value for key, value in schema.items() if key != '$defs'}
    assert set(references(schema)) == {f'#/$defs/{name}'}
    json.dumps(schema)
    value = RecursiveReply('parent', (RecursiveReply('child', (RecursiveReply('leaf'),)),))
    assert FieldCodec.decode(RecursiveReply, FieldCodec.encode(value)) == value


def test_recursive_family_and_real_problem_order_have_document_local_definitions():
    for declared in (RecursiveChoice, ProblemOrder):
        schema = FieldCodec.value_schema(declared)
        refs = set(references(schema))
        assert refs
        assert all(ref.removeprefix('#/$defs/') in schema['$defs'] for ref in refs)
        json.dumps(schema)
    value = NestedRecursiveChoice((TextRecursiveChoice('child'), NestedRecursiveChoice(())))
    assert FieldCodec.decode(RecursiveChoice, FieldCodec.encode(value)) == value
    # A separate root builds its own graph, not references into an earlier document.
    nested = FieldCodec.record_schema(NestedRecursiveChoice)
    assert set(references(nested))
    assert all(ref.removeprefix('#/$defs/') in nested['$defs'] for ref in references(nested))


def test_definition_identity_is_not_a_short_class_name():
    from dataclasses import make_dataclass

    first = make_dataclass('SameReply', [('number', int)])
    second = make_dataclass('SameReply', [('text', str)])
    root = make_dataclass('BothReplies', [('first', first), ('second', second),
                                         ('first_again', first), ('second_again', second)])
    schema = FieldCodec.value_schema(root)
    assert len(schema['$defs']) == 2
    assert len(set(references(schema))) == 2
    assert schema['properties']['first_again'] != schema['properties']['second_again']
    assert all(ref.removeprefix('#/$defs/') in schema['$defs'] for ref in references(schema))


def test_schema_failure_releases_its_document_and_new_members_are_discovered():
    @dataclass
    class BrokenReply:
        unsupported: complex

    with pytest.raises(TypeError):
        FieldCodec.value_schema(BrokenReply)
    before = FieldCodec.value_schema(RecursiveChoice)

    @dataclass
    class LaterRecursiveChoice(RecursiveChoice):
        count: int

    after = FieldCodec.value_schema(RecursiveChoice)
    assert len(after['oneOf']) == len(before['oneOf']) + 1
    assert not any('BrokenReply' in name for name in after['$defs'])


def test_native_json_schema_validator_accepts_recursive_values(native_runtime_package):
    schema = FieldCodec.value_schema(RecursiveReply)
    value = FieldCodec.encode(RecursiveReply('root', (RecursiveReply('child'),)))
    result = subprocess.run(
        ('node', '--input-type=module', '-e', '''
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {readFileSync} from 'node:fs';
const {validateToolArguments} = await import(pathToFileURL(join(process.argv[1],
  'node_modules/@earendil-works/pi-ai/dist/utils/validation.js')));
const {schema, value} = JSON.parse(readFileSync(0, 'utf8'));
const tool = {name:'recursive_reply', description:'recursive wire declaration', parameters:schema};
validateToolArguments(tool, {name:tool.name, arguments:value});
let refused = false;
try { validateToolArguments(tool, {name:tool.name,
  arguments:{...value, children:[{text:{invalid:true}}]}}); }
catch { refused = true; }
if (!refused) throw new Error('invalid nested value accepted');
''', str(native_runtime_package.path)),
        input=json.dumps(dict(schema=schema, value=value)), text=True, capture_output=True,
        timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_thread_listing_view_rejects_unknown_keys_and_wrong_activity(comms):
    from agent_comms.cli_commands import ThreadsCliCommand
    from agent_comms.presentation import ThreadRecord
    from agent_comms.tool_caller import ThreadListing

    comms.registry.declare(comms.registry.new_thread('visible', frozenset(), str(comms.root), created_at=1.0))
    records = ThreadsCliCommand().apply(comms).threads
    reply = FieldCodec.encode(ThreadListing(records, ()))
    decoded = FieldCodec.decode(ThreadListing, reply)
    assert not isinstance(decoded.threads[0], ThreadRecord)
    assert type(decoded.threads[0]) is FieldCodec.view_type(ThreadRecord, 'presence')
    assert FieldCodec.encode(decoded) == reply
    for change in ({'unexpected': True}, {'activity': 'not-a-declared-activity'}):
        invalid = {**reply, 'threads': [{**reply['threads'][0], **change}]}
        with pytest.raises(MalformedRecord):
            FieldCodec.decode(ThreadListing, invalid)
