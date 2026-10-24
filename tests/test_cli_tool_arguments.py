"""External tool spellings select one declared shape; refused calls stay as sent."""

import runpy
from abc import abstractmethod
from types import SimpleNamespace

import pytest
from metaclass_registry import DuplicateRegistryKeyError

from agent_comms import tool_calls
from agent_comms.cli_bridges.claude_protocol import ToolUse
from agent_comms.cli_bridges.opencode_parts import ToolPart
from agent_comms.native_tools import BashTool, EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from agent_comms.tool_calls import CliToolArguments, ToolCall
from agent_comms.turn_item import NativeItemId


@pytest.mark.parametrize('name,arguments,tool', [
    ('bash', {'command': 'pwd'}, BashTool),
    ('grep', {'pattern': 'needle'}, GrepTool),
    ('glob', {'pattern': '*.py'}, GlobTool),
    ('read', {'filePath': 'owner.py'}, ReadTool),
    ('edit', {'filePath': 'owner.py', 'oldString': 'before', 'newString': 'after'}, EditTool),
    ('write', {'filePath': 'owner.py', 'content': 'after'}, WriteTool),
])
def test_opencode_spellings_reach_native_records_with_extra_input(name, arguments, tool):
    arguments = {**arguments, 'provider_detail': ['retained']}
    part = ToolPart.model_validate({
        'type': 'tool', 'id': 'part', 'sessionID': 'session', 'messageID': 'message',
        'callID': 'call', 'tool': name, 'state': {'status': 'pending', 'input': arguments},
    })
    call = part.state.call(part)
    assert type(call.tool) is tool
    assert call.id == 'call'
    assert call.arguments['provider_detail'] == ['retained']
    started = call.started(NativeItemId('part'), 1.0)
    assert started.tool_call_id == call.id
    assert started.tool == call.tool


def test_claude_glob_spelling_uses_its_own_search_record():
    call = ToolUse(id='call', name='Glob', input={'pattern': '*.py'}).call()
    assert call.tool == GlobTool(pattern='*.py')


@pytest.mark.parametrize('name,arguments', [
    ('mcp__custom', {'private': ['untouched']}),
    ('BASH', {'command': 'pwd'}),
    ('Glob', {}),
    ('Bash', {'command': 17}),
    ('Grep', {'pattern': 'needle', 'path': 17}),
])
def test_unknown_or_invalid_calls_remain_the_same_original_object(name, arguments):
    sent = ToolCall('refused', name, arguments)
    assert CliToolArguments.decode(sent, arguments) is sent
    assert ToolUse(id=sent.id, name=name, input=arguments).call() == sent


@pytest.mark.parametrize('name,arguments', [
    ('external_custom', {'private': ['untouched']}),
    ('glob', {}),
    ('bash', {'command': 17}),
])
def test_opencode_unknown_or_invalid_calls_stay_as_sent(name, arguments):
    part = ToolPart.model_validate({
        'type': 'tool', 'id': 'part', 'sessionID': 'session', 'messageID': 'message',
        'callID': 'call', 'tool': name, 'state': {'status': 'pending', 'input': arguments},
    })
    assert part.state.call(part) == ToolCall('call', name, arguments)


@pytest.fixture
def argument_family():
    # Execute the actual declaration lifecycle; the canonical family stays untouched.
    return SimpleNamespace(**runpy.run_path(tool_calls.__file__, run_name=tool_calls.__name__))


def test_new_shape_is_discovered_from_its_alias_declaration(argument_family):
    class InspectArguments(argument_family.CliToolArguments):
        cli_names = ('Inspect', 'inspect')
        path: str

        def record(self, *, call_id: str) -> ReadTool:
            return ReadTool(call_id=call_id, path=self.path)

    for name in InspectArguments.cli_names:
        arguments = {'path': 'new-owner.py'}
        sent = argument_family.ToolCall('new', name, arguments)
        assert argument_family.CliToolArguments.decode(sent, arguments).tool == ReadTool(
            call_id='new', path='new-owner.py',
        )


def test_abstract_specialization_cannot_take_a_concrete_alias(argument_family):
    class IncompleteRead(argument_family.CliReadArguments):
        @abstractmethod
        def unfinished(self): ...

    with pytest.raises(TypeError, match='abstract'):
        IncompleteRead(file_path='owner.py')
    arguments = {'file_path': 'owner.py'}
    sent = argument_family.ToolCall('read', 'Read', arguments)
    assert argument_family.CliToolArguments.decode(sent, arguments).tool == ReadTool(
        call_id='read', path='owner.py',
    )


def test_duplicate_alias_group_is_rejected_by_declaration(argument_family):
    with pytest.raises(DuplicateRegistryKeyError):
        class CompetingRead(argument_family.CliReadArguments):
            pass


def test_overlapping_alias_groups_cannot_silently_choose_an_owner(argument_family):
    class OverlappingRead(argument_family.CliReadArguments):
        cli_names = ('Read', 'ReadAgain')

    for arguments in ({'file_path': 'owner.py'}, {}):
        sent = argument_family.ToolCall('ambiguous', 'Read', arguments)
        with pytest.raises(ValueError, match="Ambiguous CLI tool name 'Read'.*CliReadArguments.*OverlappingRead"):
            argument_family.CliToolArguments.decode(sent, arguments)
    arguments = {'file_path': 'owner.py'}
    sent = argument_family.ToolCall('unique', 'ReadAgain', arguments)
    assert argument_family.CliToolArguments.decode(sent, arguments).tool == ReadTool(
        call_id='unique', path='owner.py',
    )
