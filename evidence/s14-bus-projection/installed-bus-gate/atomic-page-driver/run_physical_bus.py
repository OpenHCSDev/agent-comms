"""One fresh continuous bus journey in existing private source/process custody."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import select
import sys
import time
from types import SimpleNamespace

from agent_comms.comms import Comms


stage = Path('/home/ts/.local/share/agent-comms/runtime-atomic-page-handoff-20261001')
fixture = Path('/home/ts/wt/g460b/u07')
base = Path(__file__).parent
evidence = base.parent / 'u07'
output = evidence / 'proof'
recorder_path = Path('/home/ts/.cache/agent-scratch/atomic-page-handoff-stage-20261001/source-recorder.py')
package = Path('/home/ts/.local/share/agent-comms/native-current-593b978a717ae8f6/node_modules/@earendil-works/pi-coding-agent')
driver = base / 'tests/canonical_wire_conversation_installed_pilot.py'
activation = hashlib.sha256((stage / 'activation.json').read_bytes()).hexdigest()
assert activation == 'e92534c1ec44de41db22dc1eb08bd336111454aea870e8aa56d0d7b9f3559b94'
assert not fixture.exists()
fixture.mkdir(mode=0o700)
(fixture / 'private-root').mkdir(mode=0o700)
(fixture / 'tmp').mkdir()
output.mkdir(parents=True, exist_ok=False)
comms = Comms(fixture / 'private-root/wire')
root_id = comms.messaging.initialize_private_initial_protocol()
comms.owners.pin_private_nk_launch(comms.root, root_id, package)
env = os.environ.copy()
for key in ('DISPLAY', 'NO_COLOR', 'PYTHONPATH', 'AC_CONTROLLED_READONLY_STAGE'):
    env.pop(key, None)
env.update(AGENT_COMMS_RUNTIME_ROOT=str(stage / 'bin'),
           AGENT_COMMS_ACP_LAUNCHER=str(stage / 'bin/agent-comms-acp'),
           AGENT_COMMS_ROOT=str(comms.root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
           AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
           AC_CONTROLLED_FIXTURE_STAGE=str(fixture), AC_NATIVE_COPIED_PACKAGE=str(package),
           L0A_EVIDENCE=str(output), TMPDIR=str(fixture / 'tmp'),
           TOAD_TEST_ATTEMPT='g460-u07-atomic-15601-970-6b',
           TERM='xterm-256color', COLORTERM='truecolor',
           PATH=str(stage / 'bin') + os.pathsep + env['PATH'])
spec = importlib.util.spec_from_file_location('existing_source_recorder', recorder_path)
recorder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recorder
spec.loader.exec_module(recorder)
command = [str(stage / 'bin/python'), str(driver)]
target = recorder.SourceCapture.admit(SimpleNamespace(private_root=comms.root), command, env)
owner = recorder.ProcessOwner(comms.registry)
record = {'fixture': str(fixture), 'prefix': str(stage), 'command': command,
          'root_id': root_id, 'activation_sha256': activation,
          'driver_sha256': hashlib.sha256(driver.read_bytes()).hexdigest(),
          'recorder_sha256': hashlib.sha256(recorder_path.read_bytes()).hexdigest(),
          'native_inputs_replayed': 0, 'paid_calls': 0, 'public_effects': 0}
(evidence / 'launch.json').write_text(json.dumps(record, indent=2) + '\n')
started = time.monotonic()
try:
    read_fd, write_fd = os.pipe()
    display_env = {k: v for k, v in env.items() if k != 'TOAD_TEST_ATTEMPT'}
    with (evidence / 'xvfb.log').open('w') as log:
        xvfb = owner.start(['Xvfb', '-displayfd', str(write_fd), '-screen', '0',
                           '1600x1000x24', '-nolisten', 'tcp'], env=display_env,
                          pass_fds=(write_fd,), stderr=log)
    os.close(write_fd)
    assert select.select([read_fd], [], [], 10)[0], 'Owned isolated X server did not become ready'
    display = os.read(read_fd, 30).decode().strip()
    os.close(read_fd)
    assert display.isdecimal() and int(display) != 0
    env['DISPLAY'] = ':' + display
    record['display'] = env['DISPLAY']
    with (evidence / 'terminal.log').open('w') as log:
        terminal = owner.start(['st', '-g', '160x44', '-e', *command], env=env, stderr=log)
    (evidence / 'launch.json').write_text(json.dumps(record, indent=2) + '\n')
    record['exit_code'] = terminal.process.wait(timeout=240)
finally:
    for thread in comms.registry.all_threads().values():
        if thread.pid > 0:
            comms.owners.stop(thread.name)
    record['cleanup'] = owner.cleanup()
    record['elapsed_seconds'] = time.monotonic() - started
    record['activation_unchanged'] = activation == hashlib.sha256((stage / 'activation.json').read_bytes()).hexdigest()
    (evidence / 'terminal.json').write_text(json.dumps(record, indent=2) + '\n')
assert record['exit_code'] == 0
assert record['activation_unchanged']
assert not record['cleanup']['remaining_owned_pids'] and not record['cleanup']['errors']
spec = importlib.util.spec_from_file_location('original_frame_measurement', base / 'tests/admitted_frames.py')
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
print(json.dumps(observer.review(output / 'admitted-frames.jsonl')), flush=True)
