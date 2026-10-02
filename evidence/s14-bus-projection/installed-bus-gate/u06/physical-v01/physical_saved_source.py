"""One provider-free physical saved-source observation; original fixture is immutable."""

from pathlib import Path
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys

from agent_comms.comms import Comms
from agent_comms.threads import Thread


original = Path('/home/ts/wt/g460b/u06')
fixture = Path('/home/ts/wt/g460b/v01')
output = Path('/home/ts/.cache/agent-scratch/g460-u06-physical-saved-20261001')
recorder = Path('/home/ts/wt/toad-input-delivery-visibility-20260930/tests/tools/record_installed_tui.py')
package = Path('/home/ts/.local/share/agent-comms/native-current-593b978a717ae8f6/node_modules/@earendil-works/pi-coding-agent')
evidence = Path(__file__).parent
original_root = original / 'private-root/wire'
paths = [original_root / 'registry.json', original_root / 'bus.jsonl',
         *original_root.rglob('*.jsonl'), *original.joinpath('application/pi/sessions').rglob('*.jsonl')]


def original_hashes():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


before = original_hashes()
assert not fixture.exists() and not output.exists()
fixture.mkdir(mode=0o700)
project = fixture / 'project'
project.mkdir()
config = fixture / 'pi'
config.mkdir(mode=0o700)
(fixture / 'tmp').mkdir()
for name in ('models.json', 'auth.json'):
    shutil.copyfile(original / 'application/pi' / name, config / name)
source = Comms(original_root, private_initial_writes=False,
               private_claim_writes=False).registry.require('beta').session_file
assert source is not None
saved = config / 'sessions' / Path(source).name
saved.parent.mkdir()
shutil.copyfile(source, saved)
source_hash = hashlib.sha256(Path(source).read_bytes()).hexdigest()
assert hashlib.sha256(saved.read_bytes()).hexdigest() == source_hash
comms = Comms(fixture / 'wire')
root_id = comms.messaging.initialize_private_initial_protocol()
comms.owners.pin_private_nk_launch(comms.root, root_id, package)
comms.registry.declare(Thread('beta', frozenset({'team'}), str(project),
                              session_file=str(saved), model='selected-offline/fixture',
                              thinking_level='off'))
env = os.environ.copy()
for key in ('DISPLAY', 'NO_COLOR', 'PYTHONPATH', 'PI_PROMPT', 'PI_PARENT_ID', 'PI_AGENT_ID'):
    env.pop(key, None)
env.update(TERM='xterm-256color', COLORTERM='truecolor',
           AGENT_COMMS_ROOT=str(comms.root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
           AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
           AGENT_COMMS_RUNTIME_ROOT=str(Path(sys.executable).parent),
           AGENT_COMMS_ACP_LAUNCHER=str(Path(sys.executable).with_name('agent-comms-acp')),
           AGENT_COMMS_AGENT_BIN='pi',
           AGENT_COMMS_AGENT_ARGS='--provider selected-offline --model fixture --no-extensions --no-skills --no-context-files',
           AGENT_COMMS_AGENT_MODELS='selected-offline/fixture', PI_CODING_AGENT_DIR=str(config),
           XDG_CONFIG_HOME=str(fixture / 'config'), XDG_STATE_HOME=str(fixture / 'state'),
           XDG_DATA_HOME=str(fixture / 'data'), TMPDIR=str(fixture / 'tmp'),
           AGENT_COMMS_DEBUG_LOG=str(fixture / 'acp-debug'),
           TOAD_TEST_ATTEMPT='g460-u06-provider-free-physical-v01',
           PATH=str(Path(sys.executable).parent) + os.pathsep + env['PATH'])
command = [sys.executable, str(recorder), '--capture-target', 'private',
           '--journey', 'observe', '--private-root', str(comms.root), '--output', str(output),
           '--owner', 'Schrodinger460', '--startup-wait', '8', '--max-duration', '18',
           '--tail-seconds', '2', '--fps', '8', '--width', '1600', '--height', '1000',
           '--fit-window', '--review-timing', 'deferred', '--',
           str(Path(sys.executable).with_name('toad')), 'acp',
           shlex.join([sys.executable, '-m', 'agent_comms.acp']),
           '--session', 'beta', '--project-dir', str(project)]
result = {'scope': 'Fresh private installed Toad/ACP saved native source physical color observation only; no original wire/handling recreation',
          'fixture': str(fixture), 'output': str(output), 'command': command,
          'source': source, 'source_sha256': source_hash,
          'recorder_sha256': hashlib.sha256(recorder.read_bytes()).hexdigest(),
          'terminal_env': {'TERM': env['TERM'], 'NO_COLOR_present': 'NO_COLOR' in env},
          'before_original_hashes': before}
(evidence / 'physical-saved-launch.json').write_text(json.dumps(result, indent=2) + '\n')
try:
    completed = subprocess.run(command, env=env, timeout=90)
    result['recorder_returncode'] = completed.returncode
finally:
    # The recorder excludes native owner leases from its terminal-process cleanup.
    # This is our newly declared private fixture, never the original bus.
    for thread in comms.registry.all_threads().values():
        if thread.pid > 0:
            comms.owners.stop(thread.name)
    result['after_original_hashes'] = original_hashes()
    result['original_bytes_unchanged'] = before == result['after_original_hashes']
    (evidence / 'physical-saved-terminal.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['original_bytes_unchanged']
assert result['recorder_returncode'] == 0
