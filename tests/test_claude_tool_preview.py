"""Claude's decoder publishes the shared native tool preview, not a UI special case."""
import pytest
from agent_comms.cli_bridges.claude_protocol import ClaudeSession, TimedRecord, ToolResult, ToolUse


@pytest.mark.parametrize('name,arguments,native,expected,title,patch', [
    ('Bash', {'command':'echo hello'}, 'bash', {'command':'echo hello'}, 'Run echo hello', None),
    ('Read', {'file_path':'a.py'}, 'read', {'path':'a.py'}, 'Read a.py', None),
    ('Grep', {'pattern':'needle'}, 'grep', {'pattern':'needle'}, 'Search needle', None),
    ('Edit', {'file_path':'a.py','old_string':'old\n','new_string':'new\n','replace_all':True}, 'edit',
     {'path':'a.py','edits':[{'old_string':'old\n','new_string':'new\n'}],'replace_all':True},
     'Edit a.py', '-old\n+new\n'),
    ('Write', {'file_path':'a.py','content':'new\n'}, 'write', {'path':'a.py','content':'new\n'}, 'Write a.py', '+new\n'),
    ('MultiEdit', {'file_path':'a.py','edits':[{'old_string':'a\n','new_string':'b\n'},{'old_string':'c\n','new_string':'d\n'}]}, 'edit',
     {'path':'a.py','edits':[{'old_string':'a\n','new_string':'b\n'},{'old_string':'c\n','new_string':'d\n'}]},
     'Edit a.py', '-c\n+d\n'),
    ('mcp__opaque', {'custom':['untouched']}, 'mcp__opaque', {'custom':['untouched']}, 'mcp__opaque', None),
])
def test_claude_tool_names_arguments_and_diff_reach_shared_events(name, arguments, native, expected, title, patch):
    session=ClaudeSession(turn='turn', catching_up=False)
    record=TimedRecord(uuid='record',timestamp='2026-10-17T00:00:00+00:00')
    tool=ToolUse(id='call',name=name,input=arguments)
    active,=tool.updates(record,session)
    call=session.tools['call']
    assert call.name==native and call.arguments==expected and call.title==title
    assert active.detail == title
    result=ToolResult(tool_use_id='call',content=[{'type':'text','text':'done'}])
    completed,_=result.updates(record,session)
    started,ended=completed.events
    assert started.raw_input==expected and started.title==title
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


def test_known_claude_tool_requires_its_actual_caption_input():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ToolUse(id='x',name='Bash',input={}).updates(
            TimedRecord(uuid='r',timestamp='2026-10-17T00:00:00+00:00'),ClaudeSession(turn='t'))
