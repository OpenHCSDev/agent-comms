"""One issued native MCP batch through the original AttachedChild lifetime."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


grant_path = Path(sys.argv[1])
grant_sha = sys.argv[2]
assert sha(grant_path) == grant_sha
grant = json.loads(grant_path.read_bytes())
lifecycle_bytes = Path(grant['lifecycle']).read_bytes()
lifecycle = json.loads(lifecycle_bytes)
assert lifecycle['issued_sha256'] == grant_sha
assert lifecycle['execution_authorized'] and lifecycle['native_EXEC_authorized']
assert lifecycle['control_attempts_consumed'] == 0
proposal_record = grant['proposal']
assert sha(proposal_record['path']) == proposal_record['sha256']
proposal = json.loads(Path(proposal_record['path']).read_bytes())
prefix = Path(proposal['holder']['existing_nonlive_prefix'])
output = Path(proposal['command']['owned_output'])
assert Path(sys.prefix) == prefix == Path(grant['prefix'])
assert output == Path(grant['owned_output'])
assert not os.environ.get('PYTHONPATH')
assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'
proof_path = output / 'candidate-source-proof.json'
assert sha(proof_path) == lifecycle['candidate_sourceproof_sha256']
assert json.loads(proof_path.read_bytes())['native_full_trust'] is True
for name, record in proposal['source_helpers'].items():
    if name != 'typed_consumer':
        assert sha(record['path']) == record['sha256'], name
for key, value in proposal['command']['environment']['set'].items():
    assert os.environ.get(key) == value, key
for key in proposal['command']['environment']['unset']:
    assert key not in os.environ, key
assert all(not (output / name).exists() for name in (
    'controller.json', 'child.json', 'stdout.log', 'stderr.log', 'terminal.json',
    'native.xml', 'n',
))

from agent_comms.child_process import AttachedChild, ProcessIdentity, join_retirement
from agent_comms.field_codec import FieldCodec


def write(name, value):
    with (output / name).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


async def main():
    started = time.monotonic()
    write('controller.json', {
        'identity': FieldCodec.encode(ProcessIdentity.capture(os.getpid())),
        'issued_sha256': grant_sha,
        'effective_lifecycle_sha256': hashlib.sha256(lifecycle_bytes).hexdigest(),
        'argv': proposal['command']['argv'], 'cwd': proposal['command']['cwd'],
    })
    child = None
    exchange = None
    outcome = None
    error = None
    try:
        child = await AttachedChild.start(
            tuple(proposal['command']['argv']),
            cwd=proposal['command']['cwd'], env=dict(os.environ),
        )
        write('child.json', {'identity': FieldCodec.encode(child.identity)})
        exchange = asyncio.create_task(child.process.communicate())
        await asyncio.shield(exchange)
        outcome = await child.wait()
    except BaseException as exc:
        error = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        async def retire():
            if child is not None:
                try:
                    await child.stop()
                finally:
                    if exchange is not None:
                        stdout, stderr = await join_retirement(exchange)
                        with (output / 'stdout.log').open('xb') as stream:
                            stream.write(stdout)
                        with (output / 'stderr.log').open('xb') as stream:
                            stream.write(stderr)
            write('terminal.json', {
                'outcome': FieldCodec.encode(outcome) if outcome is not None else None,
                'exit_code': child.returncode if child is not None else None,
                'error': error, 'duration_seconds': time.monotonic() - started,
                'child': FieldCodec.encode(child.identity) if child is not None else None,
                'group_remaining': [FieldCodec.encode(member) for member in
                    child.platform.group_members(child.identity)] if child is not None else [],
                'issued_sha256': grant_sha,
            })
        await join_retirement(asyncio.create_task(retire()))
    return 0 if outcome is not None and outcome.successful else 1


raise SystemExit(asyncio.run(main()))
