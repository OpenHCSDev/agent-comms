"""Claude's decoder publishes the shared native tool preview, not a UI special case."""
import pytest
from agent_comms.cli_bridges.claude_protocol import ClaudeSession, TimedRecord, ToolResult, ToolUse


@pytest.mark.parametrize('name,arguments,native,detail,patch', [
    ('Bash', {'command':'echo hello'}, 'bash', 'echo hello', None),
    ('Read', {'file_path':'a.py'}, 'read', 'a.py', None),
    ('Grep', {'pattern':'needle'}, 'grep', 'needle', None),
    ('Edit', {'file_path':'a.py','old_string':'old','new_string':'new'}, 'edit', 'a.py', '-old\n+new\n'),
    ('Write', {'file_path':'a.py','content':'new'}, 'write', 'a.py', '+new\n'),
    ('MultiEdit', {'file_path':'a.py','edits':[{'old_string':'a','new_string':'b'},{'old_string':'c','new_string':'d'}]}, 'edit', 'a.py', '-c\n+d\n'),
    ('mcp__opaque', {'custom':['untouched']}, 'mcp__opaque', '', None),
])
def test_claude_tool_names_arguments_and_diff_reach_shared_events(name, arguments, native, detail, patch):
    session=ClaudeSession(turn='turn', catching_up=False)
    record=TimedRecord(uuid='record',timestamp='2026-10-17T00:00:00+00:00')
    tool=ToolUse(id='call',name=name,input=arguments)
    active,=tool.updates(record,session)
    call=session.tools['call']
    assert call.name==native and call.arguments==arguments and call.detail==detail
    assert detail in active.detail
    result=ToolResult(tool_use_id='call',content=[{'type':'text','text':'done'}])
    completed,_=result.updates(record,session)
    started,ended=completed.events
    assert started.raw_input==arguments and started.detail==detail
    if patch is None:
        assert ended.diff is None
    else:
        assert patch in ended.diff.text
        if name=='Write':
            assert 'previous content not supplied' in ended.diff.text
    assert not session.tools


def test_known_claude_tool_requires_its_actual_caption_input():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ToolUse(id='x',name='Bash',input={}).updates(
            TimedRecord(uuid='r',timestamp='2026-10-17T00:00:00+00:00'),ClaudeSession(turn='t'))
