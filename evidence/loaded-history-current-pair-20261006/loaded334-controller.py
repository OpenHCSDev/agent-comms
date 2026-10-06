"""One issued loaded-history control through original BoundedRun/AttachedChild."""
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
assert lifecycle['native_READ_authorized']
assert lifecycle['control_attempts_consumed'] == 0
assert not lifecycle.get('source_publicdecoder_READ_authorized', False)
proposal_record = grant['proposal']
assert sha(proposal_record['path']) == proposal_record['sha256']
proposal = json.loads(Path(proposal_record['path']).read_bytes())
prefix = Path(proposal['holder']['existing_nonlive_prefix'])
output = Path(proposal['command']['owned_output'])
assert Path(sys.prefix) == prefix == Path(grant['prefix'])
assert output == Path(grant['owned_output'])
assert sha(output/'candidate-source-proof.json') == lifecycle['candidate_sourceproof_sha256']
assert json.loads((output/'candidate-source-proof.json').read_bytes())['native_full_trust'] is True
for record in proposal['source_helpers'].values():
    assert sha(record['path']) == record['sha256'], record['path']
for authority, enabled in (('matching_native_READ', False), ('matching_native_EXEC', True)):
    record = lifecycle[authority]
    assert sha(record['path']) == record['sha256']
    receipt = json.loads(Path(record['path']).read_bytes())
    assert Path(receipt['holder_grant']) == grant_path
    assert receipt['holder_grant_sha256'] == grant_sha
    assert Path(receipt['personally_read_lifecycle']) == Path(grant['lifecycle'])
    assert receipt['native_READ_authorized'] is True
    assert receipt['native_EXEC_authorized'] is enabled
    assert receipt['matching_native'] == {
        'package': proposal['native']['native_package'],
        'manifest': proposal['native']['native_manifest'],
        'tree': proposal['native']['native_tree'],
    }
for key, value in proposal['command']['environment']['set'].items():
    assert os.environ.get(key) == value, key
for key in proposal['command']['environment']['unset']:
    assert key not in os.environ, key
assert tuple(proposal['command']['argv']) == (
    str(prefix/'bin/python'), '-B', proposal['exact_control']['path'], '--loaded-histories-only')
assert all(not (output/name).exists() for name in proposal['control_new_outputs'])

from agent_comms.child_process import BoundedRun, ProcessIdentity, join_retirement
from agent_comms.field_codec import FieldCodec


def write(name, value):
    with (output/name).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


async def main():
    started = time.monotonic()
    controller = FieldCodec.encode(ProcessIdentity.capture(os.getpid()))
    (output/'effective-release.json').write_bytes(lifecycle_bytes)
    write('controller.json', {
        'identity': controller, 'issued_sha256': grant_sha,
        'effective_lifecycle_sha256': hashlib.sha256(lifecycle_bytes).hexdigest(),
        'argv': proposal['command']['argv'], 'cwd': proposal['command']['cwd'],
        'timeout_seconds': proposal['control_timeout_seconds'],
    })
    evidence = Path(proposal['command']['environment']['set']['L0A_EVIDENCE'])
    assert evidence.parent == output
    evidence.mkdir(mode=0o700)
    Path(proposal['command']['environment']['set']['TMPDIR']).mkdir(mode=0o700)
    child = exchange = outcome = None
    error = None
    timed_out = False
    drain_error = None
    try:
        async with BoundedRun.session(
                tuple(proposal['command']['argv']), timeout=proposal['control_timeout_seconds'],
                cwd=proposal['command']['cwd'], env=dict(os.environ)) as child:
            write('child.json', {'identity': FieldCodec.encode(child.identity)})
            exchange = asyncio.create_task(child.process.communicate())
            await asyncio.shield(exchange)
            outcome = await child.wait()
    except TimeoutError:
        timed_out = True
    except BaseException as failure:
        error = {'type': type(failure).__name__, 'message': str(failure)}
    finally:
        async def retire():
            nonlocal drain_error
            if child is not None:
                try:
                    await child.stop()
                finally:
                    if exchange is not None:
                        try:
                            stdout, stderr = await join_retirement(exchange)
                            (output/'stdout.log').write_bytes(stdout)
                            (output/'stderr.log').write_bytes(stderr)
                        except BaseException as failure:
                            drain_error = repr(failure)
            write('terminal.json', {
                'outcome': FieldCodec.encode(outcome) if outcome is not None else None,
                'exit_code': child.returncode if child is not None else None,
                'timed_out': timed_out, 'error': error, 'drain_error': drain_error,
                'duration_seconds': time.monotonic()-started,
                'controller': controller,
                'child': FieldCodec.encode(child.identity) if child is not None else None,
                'group_remaining': [FieldCodec.encode(member) for member in
                    child.platform.group_members(child.identity)] if child is not None else [],
                'issued_sha256': grant_sha,
                'qualification': 'Original loaded-histories receipt and raw evidence determine reached scope; no exit-only qualification.',
            })
        await join_retirement(asyncio.create_task(retire()))
    return 124 if timed_out else 0 if (
        outcome is not None and outcome.successful and error is None and drain_error is None) else 1


raise SystemExit(asyncio.run(main()))
