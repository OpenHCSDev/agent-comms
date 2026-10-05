"""Future issued index qualifier through original installed BoundedRun only."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from agent_comms.child_process import BoundedRun, ProcessIdentity
from agent_comms.field_codec import FieldCodec


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def read(path, expected):
    assert digest(path) == expected, path
    return json.loads(Path(path).read_text())


proposal_path, proposal_sha, target_grant, target_sha, source_grant, source_sha, authority, authority_sha = sys.argv[1:]
p = read(proposal_path, proposal_sha)
tg, sg = read(target_grant, target_sha), read(source_grant, source_sha)
l = json.loads(Path(tg['lifecycle']).read_text())
sl = json.loads(Path(sg['lifecycle']).read_text())
assert l['execution_authorized'] and sl['execution_authorized']
assert not l['native_EXEC_authorized'] and not sl['native_EXEC_authorized']
assert l['paired_proof_sha256'] == digest(Path(p['owned_output']) / 'candidate-source-proof.json')
assert l['control_argv'] == p['control']['argv']
assert Path(sys.prefix) == Path(p['target']['prefix']) == Path(tg['prefix'])
assert Path(sg['prefix']) == Path(p['source']['prefix'])
assert Path(sys.executable) == Path(p['target']['python'])
out = Path(p['owned_output'])
assert out == Path(tg['owned_output']) == Path(sg['owned_output'])
assert tg['operation_bound_seconds'] == sg['operation_bound_seconds'] == p['operation_bound_seconds']
for row in p['tools']:
    assert digest(row['path']) == row['sha256']
read_authority = read(authority, authority_sha)
assert read_authority['holder_grant'] == {'path': target_grant, 'sha256': target_sha}
assert read_authority['native_READ_authorized'] and not read_authority['native_EXEC_authorized']
assert read_authority['native_package'] == p['native_READ']['package']
assert read_authority['native_manifest'] == p['native_READ']['manifest']
assert read_authority['native_tree'] == p['native_READ']['tree']
environment = dict(os.environ)
for key in p['control']['environment']['unset']:
    environment.pop(key, None)
environment.update(p['control']['environment']['set'])
controller = FieldCodec.encode(ProcessIdentity.capture(os.getpid()))
command = tuple(p['control']['argv'])
with (out / 'command.json').open('x') as dest:
    dest.write(json.dumps({'argv': command, 'cwd': p['control']['cwd'], 'environment': p['control']['environment'],
                           'proposal_sha256': proposal_sha, 'controller': controller, 'timeout': tg['operation_bound_seconds'],
                           'source_grant_sha256': source_sha, 'target_grant_sha256': target_sha,
                           'native_READ_authority_sha256': authority_sha}, indent=2) + '\n')


async def drain(reader, path):
    with path.open('xb') as target:
        while data := await reader.read(65536):
            target.write(data)
            target.flush()


async def main():
    start = time.monotonic()
    child = None
    tasks = []
    timed_out = False
    error = None
    outcome = None
    try:
        async with BoundedRun.session(command, timeout=tg['operation_bound_seconds'],
                                      cwd=p['control']['cwd'], env=environment) as child:
            handle = {'controller': controller, 'child': FieldCodec.encode(child.identity),
                      'owner': 'Original installed BoundedRun.session / AttachedChild',
                      'nested_seed_declaration_writer_installer': 'Synchronous subprocess.run joins; individual nested births not captured by the existing tools'}
            with (out / 'handle.json').open('x') as dest:
                dest.write(json.dumps(handle, indent=2) + '\n')
            print('INDEX_HANDLE', json.dumps(handle), flush=True)
            tasks = [asyncio.create_task(drain(child.stdout, out / 'stdout.log')),
                     asyncio.create_task(drain(child.stderr, out / 'stderr.log'))]
            outcome = await child.wait()
    except TimeoutError:
        timed_out = True
    except BaseException as failure:
        error = repr(failure)
    finally:
        drained = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
        errors = [repr(result) for result in drained if isinstance(result, BaseException)]
        terminal = {'elapsed': time.monotonic() - start, 'returncode': None if child is None else child.returncode,
                    'timed_out': timed_out, 'error': error, 'drain_errors': errors,
                    'outcome': None if outcome is None else FieldCodec.encode(outcome),
                    'controller': controller, 'child': None if child is None else FieldCodec.encode(child.identity),
                    'child_retired': None if child is None else child.retired,
                    'owned_group_members': [] if child is None else FieldCodec.encode(child.platform.group_members(child.identity)),
                    'native_EXEC_authorized': False, 'central_batch_used': False,
                    'source_grant_sha256': source_sha, 'target_grant_sha256': target_sha}
        with (out / 'terminal.json').open('x') as dest:
            dest.write(json.dumps(terminal, indent=2) + '\n')
        print('INDEX_TERMINAL', json.dumps(terminal), flush=True)
        await asyncio.get_running_loop().shutdown_default_executor()
        return 124 if timed_out else 1 if error or errors or child is None or child.returncode is None else child.returncode


raise SystemExit(asyncio.run(main()))
