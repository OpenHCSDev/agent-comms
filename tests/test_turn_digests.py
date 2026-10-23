"""One query contract over the original native and external record owners."""

import asyncio
from contextlib import aclosing, suppress
from datetime import datetime, timedelta
import json
import os
import subprocess
import sys

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.shared import Binding, Options, Serve, Watch
from agent_comms.comms import wire
from agent_comms.errors import ToolRefusedError
from agent_comms.external_transcript import NoEarlierPage
from agent_comms.field_codec import FieldCodec
from agent_comms.runtime_updates import FinishedItem
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_text import SavedSessionFile
from agent_comms.transcript_events import AssistantTranscript, TurnDigest
from agent_comms.turn_digests import LastTurns, TurnBound, TurnsOlderThan, TurnsBefore, TurnsSince
from agent_comms.bus import Bus
from agent_comms.watched import FileSource


def prepared(tmp_path):
    comms = wire(tmp_path)
    Bus.install(Bus.path_in(comms.root))
    return comms


def endpoint(comms, name, window):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AGENT_COMMS_', 'PI_SESSION_', 'OPENCODE_SERVER_'))}
    command = [sys.executable, '-m', 'agent_comms.cli', '--root', str(comms.root), 'tool',
               '--tool', 'comms_turns', '--arguments', json.dumps(dict(thread=name, window=FieldCodec.encode(window)))]
    result = subprocess.run(command, env=environment, text=True, capture_output=True,
                            timeout=15, check=True)
    return json.loads(result.stdout)


def paged_answers(comms, name):
    window = LastTurns(100)
    seen = set()
    while True:
        answer = endpoint(comms, name, window)
        older = FieldCodec.decode(TurnBound, answer['older'])
        if not answer['turns']:
            assert older == window
            return
        assert older != window
        assert older.count == 100
        identities = [turn['turn_id'] for turn in answer['turns']]
        assert not seen.intersection(identities)
        assert len(identities) == len(set(identities))
        seen.update(identities)
        yield answer
        window = older


def test_external_turns_preserve_interleaving_and_the_whole_selected_turn(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('cli', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    source = comms.transcripts.external('cli')
    for at, (turn, item, text) in enumerate([
            ('old', 'a', 'older'), ('one', 'b', 'first'),
            ('two', 'c', 'other'), ('one', 'd', 'continued')], start=1):
        source.finish(turn_id=turn, item_id=item,
                      events=(AssistantTranscript(text, position=item, timestamp=float(at)),))
    answer = endpoint(comms, 'cli', LastTurns(2))
    assert [turn['turn_id'] for turn in answer['turns']] == ['two', 'one']
    assert [event['text'] for event in answer['turns'][0]['events']] == ['other']
    assert [event['text'] for event in answer['turns'][1]['events']] == ['first', 'continued']
    first = endpoint(comms, 'cli', LastTurns(1))
    second = endpoint(comms, 'cli', FieldCodec.decode(TurnBound, first['older']))
    third = endpoint(comms, 'cli', FieldCodec.decode(TurnBound, second['older']))
    assert [page['turns'][0]['turn_id'] for page in (first, second, third)] == ['two', 'one', 'old']
    before = endpoint(comms, 'cli', TurnsBefore(at=3.0, count=2))
    assert [turn['turn_id'] for turn in before['turns']] == ['one', 'old']
    since = endpoint(comms, 'cli', TurnsSince(3.0))
    assert [turn['turn_id'] for turn in since['turns']] == ['two', 'one']


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
    answer = endpoint(comms, 'native', LastTurns(1))
    turn, = answer['turns']
    assert turn['turn_id'] == 'u2'
    assert [event['kind'] for event in turn['events']] == ['user', 'tool_start', 'tool_end', 'assistant']
    assert turn['events'][1]['raw_input'] == dict(path='x.py')
    assert turn['events'][2]['text'] == 'contents'
    assert turn['events'][3]['text'] == 'done'
    assert [turn.turn_id for turn in comms.transcripts.turns('native', LastTurns(2)).turns] == ['u2', 'u1']
    older = FieldCodec.decode(TurnBound, answer['older'])
    assert [turn['turn_id'] for turn in endpoint(comms, 'native', older)['turns']] == ['u1']


@pytest.fixture
def recorded_claude_transcript(tmp_path):
    path = tmp_path / 'home/.claude/projects/fixture/session.jsonl'
    path.parent.mkdir(parents=True)
    start = datetime.fromisoformat('2026-10-16T12:00:00+00:00')
    rows = []
    for index in range(251):
        times = [(start + timedelta(seconds=index * 4 + phase)).isoformat() for phase in range(4)]
        prompt_id, call_id = f'prompt-{index}', f'read-{index}'
        rows.extend((
            dict(type='user', uuid=f'prompt-record-{index}', promptId=prompt_id, timestamp=times[0],
                 origin=dict(kind='human'), message=dict(content='read file')),
            dict(type='assistant', uuid=f'call-record-{index}', timestamp=times[1],
                 message=dict(content=[dict(type='tool_use', id=call_id, name='read', input=dict(path='x.py'))])),
            dict(type='user', uuid=f'result-record-{index}', promptId=prompt_id, timestamp=times[2],
                 message=dict(content=[dict(type='tool_result', tool_use_id=call_id, content='contents')])),
            dict(type='assistant', uuid=f'final-record-{index}', timestamp=times[3],
                 message=dict(content=[dict(type='text', text='done')])),
        ))
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path, rows[-1]


@pytest.mark.parametrize('empty_index', (152, 52))
def test_recorded_claude_transcript_pages_through_tool_without_loss(tmp_path, monkeypatch, recorded_claude_transcript, empty_index):
    path, final = recorded_claude_transcript
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
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
            async with asyncio.timeout(60):
                async with aclosing(Bus.watch_sources(*(
                        FileSource(item) for item in source.source_paths(comms.root)))) as changes:
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
    # Keep the original bridge recording intact. Publish its real recorded events to
    # a second isolated transcript with one eventless harness item at the page edge.
    recorded = comms.transcripts.external('claude').turns(LastTurns(300))
    comms.registry.declare(comms.registry.new_thread('paged', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    items = [FinishedItem(turn.turn_id, turn.turn_id, turn.events) for turn in reversed(recorded)]
    items.insert(empty_index, FinishedItem('undated-edge', 'undated-edge', ()))
    comms.transcripts.external('paged').publish_page(items, NoEarlierPage())
    pages = list(paged_answers(comms, 'paged'))
    seen = [turn['turn_id'] for page in pages for turn in page['turns']]
    expected = [item.turn_id for item in reversed(items)]
    assert seen == expected
    assert len(seen) == 252
    edge = seen.index('undated-edge')
    assert edge in (99, 199)
    assert pages[edge // 100]['turns'][-1]['turn_id'] == 'undated-edge'
    assert pages[edge // 100]['turns'][-1]['events'] == []
    sizes = [len(page['turns']) for page in pages]
    assert sizes == [100, 100, 52]
    print(f'Claude transcript via bridge and CommsTurnsTool: pages={sizes}, unique={len(set(seen))}, empty-edge={edge + 1}')
    turn = pages[-1]['turns'][0]
    assert turn['turn_id'] == 'prompt-51'
    assert [event['kind'] for event in turn['events']] == ['user', 'tool_start', 'tool_end', 'assistant']
    assert turn['events'][1]['raw_input'] == dict(path='x.py')
    assert turn['events'][2]['text'] == 'contents'
    assert turn['events'][3]['text'] == 'done'


def test_missing_native_session_is_empty_and_nonpositive_count_is_rejected(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('new', frozenset(), str(tmp_path)))
    for window in (LastTurns(), TurnsBefore(at=1.0, count=100), TurnsSince(1.0)):
        answer = comms.transcripts.turns('new', window)
        assert not answer.turns
        assert answer.older is window
    for window in (LastTurns(), TurnsBefore(at=1.0), TurnsOlderThan(turn_id='missing')):
        with pytest.raises(ValueError):
            FieldCodec.decode(TurnBound, {**FieldCodec.encode(window), 'count': 0})
    with pytest.raises(ToolRefusedError):
        comms.transcripts.turns('new', TurnsOlderThan(turn_id='missing'))


@pytest.mark.parametrize('events', ((), (AssistantTranscript('same time', position=1, timestamp=1.0),)))
def test_undated_and_equal_time_transcripts_page_without_timestamp_assumptions(tmp_path, events):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('undated', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    items = tuple(FinishedItem(f'undated-{index}', f'item-{index}', events) for index in range(251))
    comms.transcripts.external('undated').publish_page(items, NoEarlierPage())
    pages = list(paged_answers(comms, 'undated'))
    seen = [turn['turn_id'] for page in pages for turn in page['turns']]
    assert seen == [item.turn_id for item in reversed(items)]
    assert [len(page['turns']) for page in pages] == [100, 100, 51]


def test_turn_windows_distinguish_first_and_last_event_time():
    turn = TurnDigest('crossing', (
        AssistantTranscript('start', position=1, timestamp=1.0),
        AssistantTranscript('finish', position=2, timestamp=3.0),
    ))
    assert turn.starts_before(2.0)
    assert not turn.before(2.0)
    assert TurnsBefore(at=2.0, count=1).take((turn,)) == (turn,)
    assert TurnsSince(2.0).take((turn,)) == (turn,)
    assert not TurnDigest('empty', ()).starts_before(2.0)
    assert not TurnsBefore(at=1.0, count=1).take((turn,))
