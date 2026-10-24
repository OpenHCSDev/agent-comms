"""Native streaming: external RPC parsing and failure handling."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.agent_backend import InputContent, InputRequest, SteerInputPolicy
from agent_comms.field_codec import InputId
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_watchdog import ModelWait
from pi_session_turn import one_turn_events


def steer(text: str, input_id: InputId | None = None) -> InputRequest:
    """A queued steering input as Core hands it to the running turn."""
    return InputRequest(
        input_id=InputId.mint() if input_id is None else input_id,
        content=InputContent(text=text),
        when_busy=SteerInputPolicy,
    )


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32", reason="backend tests exec shell-script stubs; POSIX only"
    ),
]


def _stub(tmp_path: Path, body: str, name: str = "pi-stub") -> str:
    stub = tmp_path / name
    stub.write_text(body)
    stub.chmod(0o755)
    return str(stub)










class TestRpcParsing:
    def test_default_watchdog_yields_to_pi_provider_idle_authority(self):
        assert ModelWait().budget > backend._PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS






    async def test_partial_large_record_survives_cancelled_read(self):
        stream = asyncio.StreamReader(limit=8)
        reader = PiRpcChannel(stream)
        stream.feed_data(b'{"text":"' + b"x" * 100)
        pending = asyncio.create_task(reader.readline())
        while not reader.chunks:
            await asyncio.sleep(0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        stream.feed_data(b'"}\nnext\n')
        stream.feed_eof()
        assert await reader.readline() == b'{"text":"' + b"x" * 100 + b'"}\n'
        assert await reader.readline() == b"next\n"
        assert await reader.readline() == b""




    async def test_foreign_tool_use_before_exact_input_start_is_not_progress(self, tmp_path, pinned_native):
        # A typed prompt ACK is not a current-turn start. A previous Pi
        # assistant message must not escape as human-visible progress.
        stub = _stub(
            tmp_path,
            f'''#!/usr/bin/env python3
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request
import json, sys

answer_state({{'nativeInputProofCapability':'pi-native-input-v1-live-only'}})
prompt=next_prompt()
send({{'type':'response','command':'prompt','id':prompt['id'],'success':True}})
send({{'type':'message_start','message':{{'role':'assistant','content':[]}}}})
send({{'type':'message_update','assistantMessageEvent':{{'type':'text_delta','delta':'foreign earlier turn'}}}})
send({{'type':'message_end','message':{{'role':'assistant','stopReason':'toolUse','content':[{{'type':'text','text':'foreign earlier turn'}}]}}}})
send({{'type':'message_start','message':{{'role':'user','content':'foreign later turn','inputId':'foreign'}}}})
''',
        )
        events = [
            event
            async for event in one_turn_events(
                stub, [], "current prompt", str(tmp_path), package=pinned_native
            )
        ]
        assert not [event for event in events if isinstance(event, ae.CommittedProgress)]
        assert any(isinstance(event, ae.Done) and event.ok is False for event in events)
































# Native per-input proof tests retained from the PR#1 parent.
class TestNativeInputBinding:
    def test_only_original_response_can_promote_pending_attestation(self):
        from agent_comms.native_attestation import AttestationError, PendingAttestation
        from agent_comms.pi_rpc import PiRpcChannel

        pending = PendingAttestation()
        def response(identity, capability='pi-native-input-v1-live-only'):
            return PiRpcChannel.decode_record((json.dumps({
                'type': 'response', 'command': 'get_state', 'id': identity,
                'success': True, 'data': {'nativeInputProofCapability': capability},
            }) + '\n').encode(), strict=True)
        unrelated = response('another-request')
        assert pending.accept(unrelated) is pending
        assert pending.observe(unrelated.data.require_payload()) is pending
        with pytest.raises(AttestationError):
            pending.accept(response(pending.request.require_id(), 'unsupported'))
        assert pending.accept(response(pending.request.require_id())).observed
















