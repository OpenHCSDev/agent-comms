"""One granted installed invocation, using original bounded child custody."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from agent_comms.child_process import BoundedRun
from agent_comms.field_codec import FieldCodec

REPOSITORY = Path(__file__).resolve().parents[2]
EVIDENCE = Path(__file__).resolve().parent
NATIVE = '/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-086d511f2026b10d/node_modules/@earendil-works/pi-coding-agent'

async def drain(stream, path):
    with path.open('wb') as output:
        while data := await stream.read(65536):
            output.write(data)
            output.flush()

async def main():
    run, *cases = sys.argv[1:]
    if not run.isdecimal() or not cases:
        raise ValueError('An explicit run number and granted case node IDs are required')
    scratch = Path('/home/ts/.cache/agent-scratch') / ('mendel-native-source640-installed' + run)
    scratch.mkdir(exist_ok=False)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTEST_ADDOPTS', None)
    env['PI_COMPACTION_TEST_PACKAGE'] = NATIVE
    env['SELECTED_NATIVE_PROOF'] = str(EVIDENCE / ('installed-source-proof' + run + '.json'))
    command = (sys.executable, str(REPOSITORY / 'evidence/c3-turn-authority-fixtures-20261004/installed-controls.py'),
               '-o', 'addopts=', '-q', '-s', '--basetemp', str(scratch / 'private'), *cases)
    receipt = {'command': command, 'cwd': str(REPOSITORY), 'private_root': str(scratch),
               'source_head': subprocess.check_output(('git', 'rev-parse', 'HEAD'), cwd=REPOSITORY, text=True).strip(),
               'wheel_build_head': '2b3e08bb5fe4b754caa324da4343918db437eef8',
               'native': NATIVE, 'external_provider_inputs': 0, 'public_inputs': 0,
               'replayed_inputs': 0, 'timeout_seconds': 480}
    (EVIDENCE / ('installed-invocation' + run + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
    started = time.monotonic()
    child = None
    drains = []
    try:
        async with BoundedRun.session(command, timeout=480, cwd=REPOSITORY, env=env) as child:
            receipt['identity'] = FieldCodec.encode(child.identity)
            (EVIDENCE / ('live-handle' + run + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps({'live_identity': receipt['identity'], 'log': str(EVIDENCE / ('installed' + run + '.log'))}), flush=True)
            drains = [asyncio.create_task(drain(child.stdout, EVIDENCE / ('installed' + run + '.log'))),
                      asyncio.create_task(drain(child.stderr, EVIDENCE / ('installed' + run + '.stderr.log')))]
            receipt['outcome'] = FieldCodec.encode(await child.wait())
            receipt['returncode'] = child.returncode
            await asyncio.gather(*drains)
    except BaseException as error:
        receipt['error'] = {'type': type(error).__name__, 'message': str(error)}
        receipt['traceback'] = traceback.format_exc()
    finally:
        if drains:
            await asyncio.gather(*drains, return_exceptions=True)
        receipt['elapsed_seconds'] = time.monotonic() - started
        if child is not None:
            receipt['controller_retired'] = child.retired
            receipt['remaining_group_members'] = [FieldCodec.encode(item) for item in child.platform.group_members(child.identity)]
        (EVIDENCE / ('terminal' + run + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps({'returncode': receipt.get('returncode'), 'error': receipt.get('error'),
                          'elapsed_seconds': receipt['elapsed_seconds'], 'controller_retired': receipt.get('controller_retired')}), flush=True)
    return 0 if receipt.get('returncode') == 0 and receipt.get('controller_retired') else 1

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
