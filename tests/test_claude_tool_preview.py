"""Claude's decoder publishes the shared native tool preview, not a UI special case."""
import pytest
from agent_comms.cli_bridges.claude_protocol import ClaudeSession, KnownTurn, Live, TimedRecord, ToolResult, ToolUse
from agent_comms.tool_calls import ToolCall
from agent_comms.field_codec import FieldCodec
from agent_comms.native_tools import BashTool


@pytest.mark.parametrize('name,arguments,native,expected,title,patch', [
    ('Bash', {'command':'echo hello'}, 'bash', {'command':'echo hello'}, 'Run echo hello', None),
    ('Read', {'file_path':'a.py'}, 'read', {'path':'a.py'}, 'Read a.py', None),
    ('Grep', {'pattern':'needle'}, 'grep', {'pattern':'needle'}, 'Search needle', None),
    ('Edit', {'file_path':'a.py','old_string':'old\n','new_string':'new\n','replace_all':True}, 'edit',
     {'path':'a.py','old_string':'old\n','new_string':'new\n','replace_all':True},
     'Edit a.py', '-old\n+new\n'),
    ('Write', {'file_path':'a.py','content':'new\n'}, 'write', {'path':'a.py','content':'new\n'}, 'Write a.py', '+new\n'),
    ('MultiEdit', {'file_path':'a.py','edits':[{'old_string':'a\n','new_string':'b\n'},{'old_string':'c\n','new_string':'d\n'}]}, 'edit',
     {'path':'a.py','edits':[{'old_string':'a\n','new_string':'b\n'},{'old_string':'c\n','new_string':'d\n'}]},
     'Edit a.py', '-c\n+d\n'),
    ('mcp__opaque', {'custom':['untouched']}, 'mcp__opaque', {'custom':['untouched']}, 'mcp__opaque', None),
])
def test_claude_tool_names_arguments_and_diff_reach_shared_events(name, arguments, native, expected, title, patch):
    session=ClaudeSession(turn=KnownTurn('turn'), phase=Live())
    record=TimedRecord(position=0,uuid='record',timestamp='2026-10-17T00:00:00+00:00')
    tool=ToolUse(id='call',name=name,input=arguments)
    active,=tool.updates(record,session)
    call,source=session.tools['call']
    assert source is record
    assert call.name==native and call.arguments==expected and call.title==title
    assert active.detail == title
    result=ToolResult(tool_use_id='call',content=[{'type':'text','text':'done'}])
    completed,_=result.updates(record,session)
    started,ended=completed.events
    assert started.raw_input==expected and started.title==title
    assert started.timestamp == ended.timestamp == record.timestamp
    if patch is None:
        assert ended.diff is None
    else:
        assert patch in ended.diff.text
        if name=='Write':
            assert 'previous content not supplied' in ended.diff.text
    assert not session.tools


def test_diff_constructor_preserves_a_newline_only_change():
    from agent_comms.tool_results import ToolDiff
    patch = ToolDiff.from_versions('a.py', 'old', 'old\n')
    assert '-old\n\\ No newline at end of file\n+old\n' in patch.text


def test_known_claude_tool_with_input_that_does_not_fit_is_recorded_as_sent():
    # Claude records calls the model sent with invalid input (a real Glob call with {})
    # and answers them with an error result; the call is shown under the name Claude used.
    assert ToolUse(id='x', name='Glob', input={}).call() == ToolCall('x', 'Glob', {})
    assert ToolUse(id='y', name='Bash', input={'command': 'ls'}).call().name == FieldCodec.encode(BashTool)
