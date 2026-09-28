"""Actual native RPC startup on retained proof; no model/provider request."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

package = Path(os.environ['PI_NATIVE_PACKAGE_DIR']).resolve()
root = Path(tempfile.mkdtemp(prefix='native-cli-proof-', dir=Path('.artifacts/tmp').resolve()))
try:
    session = root / 'session.jsonl'
    identity = str(uuid.uuid4())
    input_id = '1' * 32
    request = {'kind': 'prompt', 'text': 'retained', 'images': None, 'source': 'rpc'}
    digest = hashlib.sha256(('pi-input-request-v1\n' + json.dumps(request, separators=(',', ':'))).encode()).hexdigest()
    rows = [
        {'type': 'session', 'version': 3, 'id': identity, 'cwd': str(root), 'timestamp': '2026-09-28T00:00:00.000Z'},
        {'type': 'message', 'id': '00000001', 'parentId': None, 'message': {
            'role': 'user', 'content': [{'type': 'text', 'text': 'retained'}],
            'timestamp': 0, 'inputId': input_id, 'inputDigest': digest}},
        {'type': 'message', 'id': '00000002', 'parentId': '00000001', 'message': {
            'role': 'user', 'content': [{'type': 'text', 'text': 'current'}], 'timestamp': 0}},
        {'type': 'compaction', 'id': '00000003', 'parentId': '00000002',
            'summary': 'retained summary', 'firstKeptEntryId': '00000002', 'tokensBefore': 12},
    ]
    session.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    session.chmod(0o600)
    proof = Path(str(session) + '.input-proof')
    row = {'schema': 1, 'type': 'context_committed', 'sessionId': identity,
           'inputId': input_id, 'sessionEntryId': '00000001', 'requestGeneration': 0,
           'llmContextDigest': 'b' * 64}
    count = 0
    with proof.open('w') as output:
        while output.tell() <= 256 * 1024 * 1024:
            count += 1
            row['requestGeneration'] = count
            output.write(json.dumps(row, separators=(',', ':')) + '\n')
    proof.chmod(0o600)
    size = proof.stat().st_size
    env = {key: value for key, value in os.environ.items() if key not in ('NODE_OPTIONS', 'NODE_PATH')}
    env.update(PI_CODING_AGENT_DIR=str(root/'agent'), AGENT_COMMS_SESSION_INDEX_DIR=str(root/'indexes'))
    guard = root / 'no-network.mjs'
    guard.write_text('''import http from 'node:http'; import https from 'node:https';
import net from 'node:net'; import {syncBuiltinESMExports} from 'node:module';
const deny=()=>{throw new Error('Provider/network forbidden in proof startup acceptance');};
http.request=http.get=https.request=https.get=net.connect=net.createConnection=deny;
globalThis.fetch=deny; syncBuiltinESMExports();
''')
    completed = subprocess.run(['node', '--max-old-space-size=96', '--import', str(guard),
        str(package/'dist/cli.js'), '--mode', 'rpc', '--session', str(session),
        '--session-dir', str(root), '--no-extensions', '--no-skills', '--no-prompt-templates'],
        input=json.dumps({'type': 'get_state', 'id': 'proof-startup'})+'\n',
        capture_output=True, text=True, env=env, cwd=root, timeout=75)
    if completed.returncode:
        raise RuntimeError(completed.stderr[-8000:] + completed.stdout[-2000:])
    records = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{')]
    state = next(row for row in records if row.get('id') == 'proof-startup')
    assert state['success'] is True, state
    assert not any(row.get('type') in {'input_committed', 'context_committed'} for row in records)
    assert proof.stat().st_size == size
    print(json.dumps({'nativeCLI': True, 'journalBytes': size, 'generations': count,
                      'stateSuccess': True, 'liveProofEmissions': 0, 'heapMiB': 96}))
finally:
    shutil.rmtree(root)
