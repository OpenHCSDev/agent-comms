import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from agent_comms.child_process import BoundedRun, Platform, ProcessIdentity
from agent_comms.field_codec import FieldCodec

issued = Path(sys.argv[1])
sha = lambda data: hashlib.sha256(data).hexdigest()
assert sha(issued.read_bytes()) == sys.argv[2]
grant = json.loads(issued.read_text())
lifecycle_path = Path(grant['lifecycle'])
lifecycle_bytes = lifecycle_path.read_bytes()
lifecycle = json.loads(lifecycle_bytes)
phase = 'native_batch'
assert Path(sys.prefix) == Path(grant['prefix'])
assert not os.environ.get('PYTHONPATH')
assert lifecycle['execution_authorized'] and lifecycle['native_EXEC_authorized']
assert lifecycle['native_attempts_consumed'] == 0
exec_grant = Path(lifecycle['native_EXEC_authority']['path'])
assert sha(exec_grant.read_bytes()) == lifecycle['native_EXEC_authority']['sha256']
assert lifecycle['native_batch'] == grant['native_batch']
for relative, expected in grant['control_sha256'].items():
    assert sha((Path(grant[phase]['cwd']) / relative).read_bytes()) == expected
specification = grant[phase]
output = Path(specification['output'])
assert not output.exists()
output.mkdir(parents=True)
env = dict(os.environ)
for name, value in specification['environment'].items():
    if value is None:
        env.pop(name, None)
    else:
        env[name] = value
platform = Platform.current()

async def main():
    started = time.monotonic()
    observed = {}
    stdout, stderr = b'', b''
    outcome = None
    error = None
    child = None
    exchange = None
    try:
        async with BoundedRun.session(tuple(specification['argv']), timeout=120,
                                      cwd=specification['cwd'], env=env) as child:
            observed[child.pid] = child.identity
            (output / 'live-handle.json').write_text(json.dumps({
                'controller': FieldCodec.encode(child.identity), 'phase': phase,
                'lifecycle_sha256': sha(lifecycle_bytes), 'argv': specification['argv'],
                'output': str(output), 'started_monotonic': started,
            }, indent=2) + '\n')
            exchange = asyncio.create_task(child.process.communicate())
            while not exchange.done():
                # Observe only descendants of the original acquired controller;
                # identities and group retirement remain the existing process owner.
                for identity in tuple(observed.values()):
                    if not identity.alive():
                        continue
                    tasks = Path(f'/proc/{identity.pid}/task')
                    try:
                        task_paths = list(tasks.iterdir())
                    except FileNotFoundError:
                        continue
                    for task in task_paths:
                        try:
                            children = (task / 'children').read_text().split()
                        except (FileNotFoundError, ProcessLookupError):
                            continue
                        for pid_text in children:
                            try:
                                captured = ProcessIdentity.capture(int(pid_text))
                            except ProcessLookupError:
                                continue
                            observed[captured.pid] = captured
                await asyncio.sleep(0.05)
            stdout, stderr = await exchange
            outcome = await child.wait()
    except BaseException as caught:
        error = repr(caught)
        if exchange is not None:
            stdout, stderr = await exchange
    finally:
        (output / 'stdout.log').write_bytes(stdout)
        (output / 'stderr.log').write_bytes(stderr)
        groups = {str(identity.pid): FieldCodec.encode(platform.group_members(identity)) for identity in observed.values()}
        alive = [FieldCodec.encode(identity) for identity in observed.values() if identity.alive()]
        sockets = [str(path) for path in output.rglob('*') if path.is_socket()]
        terminal = {'phase': phase, 'issued_sha256': sha(issued.read_bytes()),
                    'lifecycle_sha256_at_launch': sha(lifecycle_bytes), 'argv': specification['argv'],
                    'outcome': FieldCodec.encode(outcome) if outcome is not None else None,
                    'elapsed_seconds': time.monotonic() - started, 'error': error,
                    'controller': FieldCodec.encode(child.identity) if child else None,
                    'observed_children': [FieldCodec.encode(identity) for identity in observed.values()],
                    'alive': alive, 'groups': groups, 'sockets': sockets,
                    'observation_limit': 'Short-lived descendants may exit between read-only /proc observations; original fixture/process custody joins its actual children.'}
        (output / 'terminal.json').write_text(json.dumps(terminal, indent=2) + '\n')
        print(json.dumps(terminal), flush=True)
    return 0 if error is None and outcome is not None and outcome.successful and not alive and not any(groups.values()) and not sockets else 1

raise SystemExit(asyncio.run(main()))
