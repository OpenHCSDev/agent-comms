"""Recorded Pi RPC child. Only the external model/process edge is controlled."""
import json
import os
import sys
import time
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows
from pi_stub_protocol import read_request, request_response, send
from agent_comms.private_sidecar import native_request_digest

control = Path(sys.argv[1])
args = sys.argv[2:]
folder = Path(args[args.index('--session-dir') + 1])
session = Path(args[args.index('--session') + 1]) if '--session' in args else folder / 'scripted.jsonl'
session.parent.mkdir(parents=True, exist_ok=True)
if not session.exists():
    session.write_text(json.dumps({'type': 'session', 'id': 'scripted-owner', 'version': 3}) + '\n')
session.chmod(0o600)
state = {'nativeInputProofCapability': 'pi-native-input-v1-live-only',
         'sessionId': 'scripted-owner', 'sessionFile': str(session),
         'model': {'provider': 'test', 'id': 'model', 'contextWindow': 200000},
         'thinkingLevel': 'off', 'isStreaming': False}
count = 0
while True:
    line = sys.stdin.readline()
    if not line:
        break
    request = read_request(line)
    kind = request['type']
    if kind == 'get_state':
        send(request_response(request, state))
    elif kind == 'get_session_stats':
        send(request_response(request, {'sessionId': 'scripted-owner', 'sessionFile': str(session)}))
    elif kind == 'prompt':
        count += 1
        with (control / 'requests.jsonl').open('a') as stream:
            stream.write(json.dumps(request) + '\n')
        if count == 1:
            (control / 'entered').touch()
            deadline = time.monotonic() + 20
            while not (control / 'release').exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Parent did not release the scripted selected input')
                time.sleep(.005)
        native_id = request['inputId']
        digest = native_request_digest(
            request['message'], streaming_behavior=request.get('streamingBehavior'),
        )
        entry = {'type': 'message', 'id': native_id, 'message': {
            'role': 'user', 'inputId': native_id, 'inputDigest': digest,
            'content': request['message']}}
        with session.open('a') as stream:
            stream.write(json.dumps(entry) + '\n')
        prior = read_proof_rows(session) if Path(str(session) + '.input-proof').exists() else []
        proof = {'schema': 2, 'type': 'context_committed', 'sessionId': 'scripted-owner',
                 'inputId': native_id, 'sessionEntryId': native_id,
                 'requestGeneration': len(prior) + 1, 'llmContextDigest': 'c' * 64,
                 'committedAtMs': 1791964800000 + count}
        write_proof_rows(session, [*prior, proof])
        send(request_response(request, {}))
        send({'type': 'input_committed', 'sessionId': 'scripted-owner',
              'inputId': native_id, 'sessionEntryId': native_id})
        send({'type': 'message_start', 'message': {'role': 'user',
              'inputId': native_id, 'content': request['message']}})
        send(proof)
        failed = count > 1 and (control / 'fail').exists()
        text = 'Selected response' if count == 1 else 'Fresh owner input completed'
        send({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': text}})
        send({'type': 'message_end', 'message': {'role': 'assistant',
              'content': [{'type': 'text', 'text': text}],
              'stopReason': 'error' if failed else 'stop',
              **({'errorMessage': 'Scripted provider failure'} if failed else {})}})
        send({'type': 'agent_settled'})
    else:
        send(request_response(request, {}))
