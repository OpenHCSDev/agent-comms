"""Committed native output proves one persisted row, not an entire live cohort."""
import json
from dataclasses import replace

import pytest

from agent_comms.acp_extension import AgentCommsUpdate, NativeOutputCommittedUpdate
from agent_comms.coordination_errors import TranscriptChanged
from agent_comms.field_codec import FieldCodec
from agent_comms.native_output import AssistantOutput, ToolOutput
from agent_comms.native_tool_call import ToolCallId
from agent_comms.pi_events import NativeOutputCommitted, PiEvent
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.threads import Thread
from conftest import installed_comms


def append(path, value):
    start = path.stat().st_size if path.exists() else 0
    with path.open('ab') as stream:
        stream.write(json.dumps(value).encode() + b'\n')
    return start, path.stat().st_size


@pytest.fixture
def committed(tmp_path):
    comms = installed_comms(tmp_path / 'wire')
    path = tmp_path / 'session.jsonl'
    append(path, dict(type='session', id='session-one', version=3,
                     timestamp='2026-10-26T00:00:00Z'))
    start, end = append(path, dict(type='message', id='assistant-one',
        timestamp='2026-10-26T00:00:01Z', message=dict(role='assistant',
        content=[dict(type='text', text='Original saved answer π')])) )
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path),
        session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    marker = PiEvent.from_wire(dict(type='native_output_committed',
        sessionId='session-one', sessionFile=str(path), sessionEntryId='assistant-one',
        sessionEntryStart=start, sessionEntryEnd=end, output=dict(kind='assistant')))
    assert isinstance(marker, NativeOutputCommitted)
    return comms, path, marker


def test_output_proof_covers_only_original_prefix_and_survives_later_appends(committed):
    comms, path, marker = committed
    proof = comms.transcripts.capture_native_output('worker', marker)
    append(path, dict(type='message', id='later', timestamp='2026-10-26T00:00:02Z',
                     message=dict(role='assistant', content=[dict(type='text', text='Later output')])))
    fresh = comms.transcripts.capture_page_read('worker').identity
    assert proof.page_bound.offset == marker.session_entry_end < fresh.page_bound.offset
    assert proof.contains_native_prefix(fresh)
    assert not proof.contains_native_prefix(replace(fresh, through=fresh.page_bound.at_offset(marker.session_entry_end - 1)))
    assert not proof.contains_native_prefix(replace(fresh, root='/different/source'))
    assert not proof.contains_native_prefix(replace(fresh, thread=replace(fresh.thread,
        created_at=fresh.thread.created_at.to_wire() + 1)))
    update = NativeOutputCommittedUpdate(proof, marker.output)
    assert FieldCodec.decode(AgentCommsUpdate, FieldCodec.encode(update)) == update
    replacement = path.with_suffix('.replacement')
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    assert not proof.contains_native_prefix(comms.transcripts.capture_page_read('worker').identity)


@pytest.mark.parametrize('change', [
    dict(session_id='wrong-session'), dict(session_entry_id='wrong-row'),
    dict(output=ToolOutput(ToolCallId('wrong-tool'))),
    dict(session_entry_start=1), dict(session_entry_end=2),
])
def test_output_capture_refuses_wrong_original_coordinates(committed, change):
    comms, path, marker = committed
    with pytest.raises((ValueError, TranscriptChanged)):
        comms.transcripts.capture_native_output('worker', replace(marker, **change))


def test_tool_checkpoint_retains_original_call_identity(committed):
    comms, path, marker = committed
    start, end = append(path, dict(type='message', id='tool-result',
        timestamp='2026-10-26T00:00:02Z', message=dict(role='toolResult',
            toolCallId='call-original', toolName='read', isError=False,
            content=[dict(type='text', text='Tool result')])) )
    marker = replace(marker, session_entry_id='tool-result', session_entry_start=start,
                     session_entry_end=end, output=ToolOutput(ToolCallId('call-original')))
    proof = comms.transcripts.capture_native_output('worker', marker)
    assert proof.page_bound.offset == end
    with pytest.raises(TranscriptChanged):
        comms.transcripts.capture_native_output('worker', replace(marker, output=ToolOutput(ToolCallId('parallel-call'))))
    with pytest.raises(TranscriptChanged):
        comms.transcripts.capture_native_output('worker', replace(marker, output=AssistantOutput()))


def test_sdk_emits_scoped_marker_after_original_append(pinned_native, tmp_path):
    import subprocess
    from pathlib import Path

    result = subprocess.run([
        'node', str(Path(__file__).parent / 'fixtures/pi_helpers/output_commit.mjs'),
        str(pinned_native), str(tmp_path / 'sdk'),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    markers = json.loads(result.stdout)
    assert [type(PiEvent.from_wire(marker).output) for marker in markers] == [AssistantOutput, ToolOutput]
