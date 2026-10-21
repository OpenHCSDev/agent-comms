"""One query contract over the original native and external record owners."""

import asyncio
from contextlib import aclosing, suppress
from datetime import datetime
import json
import os
import subprocess
import sys

import pytest

from agent_comms.comms import wire
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.turn_digests import LastTurns
from agent_comms.thread_text import SavedSessionFile


def prepared(tmp_path):
    comms = wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    return comms


def endpoint(comms, name, last):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AGENT_COMMS_', 'PI_SESSION_', 'OPENCODE_SERVER_'))}
    command = [sys.executable, '-m', 'agent_comms.cli', '--root', str(comms.root), 'tool',
               '--tool', 'comms_turns', '--arguments', json.dumps(dict(thread=name, last=last))]
    result = subprocess.run(command, env=environment, text=True, capture_output=True,
                            timeout=15, check=True)
    return json.loads(result.stdout)


def test_external_turns_preserve_interleaving_and_the_whole_selected_turn(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('cli', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    source = comms.transcripts.external('cli')
    for at, (turn, item, text) in enumerate([
            ('old', 'a', 'older'), ('one', 'b', 'first'),
            ('two', 'c', 'other'), ('one', 'd', 'continued')], start=1):
        source.finish(turn_id=turn, item_id=item,
                      events=(AssistantTranscript(text, position=item, timestamp=float(at)),))
    answer = endpoint(comms, 'cli', 2)
    assert [turn['turn_id'] for turn in answer['turns']] == ['one', 'two']
    assert [event['text'] for event in answer['turns'][0]['events']] == ['first', 'continued']
    assert [event['text'] for event in answer['turns'][1]['events']] == ['other']


def test_native_turns_keep_original_calls_results_and_input_identity(tmp_path):
    comms = prepared(tmp_path)
    source = tmp_path / 'native.jsonl'
    rows = [
        dict(type='session', id='source', version=3),
        dict(type='message', id='u1', timestamp='2026-10-16T12:00:00Z',
             message=dict(role='user', content='old input')),
        dict(type='message', id='a1', parentId='u1', timestamp='2026-10-16T12:00:01Z', message=dict(
            role='assistant', content=[dict(type='text', text='old answer')], stopReason='stop')),
        dict(type='message', id='u2', parentId='a1', timestamp='2026-10-16T12:00:02Z',
             message=dict(role='user', content='read file')),
        dict(type='message', id='call', parentId='u2', timestamp='2026-10-16T12:00:03Z', message=dict(
            role='assistant', content=[dict(type='toolCall', id='read-1', name='read',
                                            arguments=dict(path='x.py'))], stopReason='toolUse')),
        dict(type='message', id='result', parentId='call', timestamp='2026-10-16T12:00:04Z', message=dict(
            role='toolResult', toolCallId='read-1', toolName='read', isError=False,
            content=[dict(type='text', text='contents')])),
        dict(type='message', id='a2', parentId='result', timestamp='2026-10-16T12:00:05Z', message=dict(
            role='assistant', content=[dict(type='text', text='done')], stopReason='stop')),
    ]
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    source.chmod(0o600)
    comms.registry.declare(comms.registry.new_thread('native', frozenset(), str(tmp_path), session_file=SavedSessionFile(str(source))))
    answer = endpoint(comms, 'native', 1)
    turn, = answer['turns']
    assert turn['turn_id'] == 'u2'
    assert [event['kind'] for event in turn['events']] == ['user', 'tool_start', 'tool_end', 'assistant']
    assert turn['events'][1]['raw_input'] == dict(path='x.py')
    assert turn['events'][2]['text'] == 'contents'
    assert turn['events'][3]['text'] == 'done'
    assert [turn.turn_id for turn in comms.transcripts.turns('native', LastTurns(2)).turns] == ['u2', 'u1']


def test_claude_file_transport_reaches_the_same_cli_and_tool_answer(tmp_path, monkeypatch):
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.cli_bridges.claude import ClaudeBridge
    from agent_comms.cli_bridges.shared import Binding, Options, Serve, Watch
    from agent_comms.wire_watch import WatchSource, WireWatch

    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    path = home / '.claude/projects/fixture/session.jsonl'
    path.parent.mkdir(parents=True)
    prompt = dict(type='user', uuid='prompt-record', promptId='prompt', timestamp='2026-10-16T12:00:00Z',
                  origin=dict(kind='human'), message=dict(content='read file'))
    call = dict(type='assistant', uuid='call-record', timestamp='2026-10-16T12:00:01Z',
                message=dict(content=[dict(type='tool_use', id='read-1', name='read', input=dict(path='x.py'))]))
    result = dict(type='user', uuid='result-record', promptId='prompt', timestamp='2026-10-16T12:00:02Z',
                  message=dict(content=[dict(type='tool_result', tool_use_id='read-1', content='contents')]))
    final = dict(type='assistant', uuid='final-record', timestamp='2026-10-16T12:00:03Z',
                 message=dict(content=[dict(type='text', text='done')]))
    path.write_text(''.join(json.dumps(row) + '\n' for row in (prompt, call, result, final)))
    comms = prepared(tmp_path / 'wire')
    comms.registry.declare(comms.registry.new_thread('claude', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    state = tmp_path / 'state'
    store = Binding.open(state)
    db = store.__enter__()
    binding = Binding(str(comms.root), 'claude', 'session')
    with pytest.raises(ValueError):
        Watch().transcript_source()
    operation = Serve(owner=ProcessIdentity.capture(os.getpid()), transcript=path)
    bridge = ClaudeBridge(Options(operation, binding, state), binding, db)

    async def publish():
        task = asyncio.create_task(bridge.publish_transcript())
        source = comms.transcripts.external('claude')
        try:
            async with asyncio.timeout(10):
                async with aclosing(WireWatch.observations(*(
                        WatchSource.modified_file(item) for item in source.source_paths(comms.root)))) as changes:
                    async for _ in changes:
                        turns = await asyncio.to_thread(comms.transcripts.turns, 'claude', LastTurns())
                        if any(event == AssistantTranscript('done', position=event.position,
                                                                 timestamp=datetime.fromisoformat(final['timestamp']).timestamp())
                               for turn in turns.turns for event in turn.events):
                            break
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            store.__exit__(None, None, None)

    asyncio.run(publish())
    answer = endpoint(comms, 'claude', 1)
    turn, = answer['turns']
    assert turn['turn_id'] == 'prompt'
    assert [event['kind'] for event in turn['events']] == ['user', 'tool_start', 'tool_end', 'assistant']
    assert turn['events'][1]['raw_input'] == dict(path='x.py')
    assert turn['events'][2]['text'] == 'contents'
    assert turn['events'][3]['text'] == 'done'


def test_missing_native_session_is_empty_and_nonpositive_count_is_rejected(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('new', frozenset(), str(tmp_path)))
    assert not comms.transcripts.turns('new', LastTurns()).turns
    with pytest.raises(ValueError):
        comms.transcripts.turns('new', LastTurns(0))
