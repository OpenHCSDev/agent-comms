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
    from agent_comms.field_codec import _SchemaScope
    with pytest.raises(RuntimeError, match='active document'):
        _SchemaScope.current.get().declared(str, lambda: {})


def assert_native_validation(package, schema, value, invalid):
    result = subprocess.run(
        ('node', '--input-type=module', '-e', '''
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {readFileSync} from 'node:fs';
const {validateToolArguments} = await import(pathToFileURL(join(process.argv[1],
  'node_modules/@earendil-works/pi-ai/dist/utils/validation.js')));
const {schema, value, invalid} = JSON.parse(readFileSync(0, 'utf8'));
const tool = {name:'recursive_reply', description:'recursive wire declaration', parameters:schema};
validateToolArguments(tool, {name:tool.name, arguments:value});
let refused = false;
try { validateToolArguments(tool, {name:tool.name,
  arguments:invalid}); }
catch { refused = true; }
if (!refused) throw new Error('invalid nested value accepted');
''', str(package.path)),
        input=json.dumps(dict(schema=schema, value=value, invalid=invalid)), text=True, capture_output=True,
        timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_native_json_schema_validator_accepts_recursive_values(native_runtime_package):
    schema = FieldCodec.value_schema(RecursiveReply)
    value = FieldCodec.encode(RecursiveReply('root', (RecursiveReply('child'),)))
    assert_native_validation(native_runtime_package, schema, value,
                             {**value, 'children': [{'text': {'invalid': True}}]})


def test_thread_listing_view_rejects_unknown_keys_and_wrong_activity(comms, native_runtime_package):
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
        assert_native_validation(native_runtime_package, FieldCodec.value_schema(ThreadListing),
                                 reply, invalid)
    missing = {**reply, 'threads': [{key: value for key, value in reply['threads'][0].items()
                                   if key != 'activity_detail'}]}
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(ThreadListing, missing)


def test_history_result_members_preserve_declared_wire_shapes(native_runtime_package):
    from dataclasses import replace
    from agent_comms.history_result import (ChannelHistoryPageResult, DmHistoryPageResult,
                                            EverythingHistoryPageResult, HistoryPageResult)
    from agent_comms.message_provenance import HistoryMessageProvenance
    from agent_comms.messages import Message, MessageKind
    from agent_comms.page_traversal import HistoryCursor, NoPageCursor
    from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
    from agent_comms.recorded_bus import NoRecordedBus

    message = Message(sender='lead', target='worker', body='hello',
                      type=MessageKind.member_named('info'), timestamp=10.0, seq=3)
    for member, name, key in ((DmHistoryPageResult, 'worker', 'dm'),
                              (EverythingHistoryPageResult, '#any', 'everything'),
                              (ChannelHistoryPageResult, '#team', 'messages')):
        page = member.for_target(name, (message,), False)
        expected = {key: [message.to_wire()], 'before': 3, 'has_older': False}
        if member is ChannelHistoryPageResult:
            expected['channel'] = name
        assert FieldCodec.encode(page) == expected
        received = FieldCodec.decode(HistoryPageResult, expected)
        assert type(received) is member
        assert FieldCodec.encode(received) == expected

    empty = DmHistoryPageResult.for_target('worker', (), False)
    assert isinstance(empty.before, NoPageCursor)
    assert FieldCodec.encode(empty) == {'dm': [], 'before': None, 'has_older': False}
    assert FieldCodec.encode(FieldCodec.decode(HistoryPageResult, FieldCodec.encode(empty))) == FieldCodec.encode(empty)
    provenance = HistoryMessageProvenance('/original', 'a' * 32, NoRecordedBus(),
                                          UnrecordedEventTime(), RecordedEventTime(1.0))
    archived = DmHistoryPageResult(
        (replace(DmHistoryPageResult.for_target('worker', (message,), False).messages[0],
                 history=provenance),), False, before=HistoryCursor('archive', 3))
    wire = FieldCodec.encode(archived)
    assert wire['dm'][0]['history'] == FieldCodec.encode(provenance)
    assert FieldCodec.encode(FieldCodec.decode(HistoryPageResult, wire)) == wire
    invalid = {**wire, 'dm': [{**wire['dm'][0], 'history': {'unrecognized': True}}]}
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(HistoryPageResult, invalid)
    assert_native_validation(native_runtime_package, FieldCodec.value_schema(HistoryPageResult),
                             wire, invalid)
