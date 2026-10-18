"""doctrine-segment: one bounded physical #777 check; no live-root writes."""
from pathlib import Path
import json
import os
import shlex
import subprocess
import time

checkout = Path('/home/ts/wt/core-doctrine-segment')
root = checkout / '.scratch/goal-physical-20261013-final'
root.mkdir(mode=0o700, exist_ok=False)
(root / 'owner-purpose.json').write_text(json.dumps({
    'owner': 'doctrine-segment', 'purpose': 'physical st completion of merged #777 acceptance',
    'source': 'verify/goal-physical', 'display': ':191',
    'retention': 'concise receipt and stills pending reviewer acceptance; preserve native sessions/dispositions',
    'no_live_bus_inputs': True, 'no_paid_provider': True}, indent=2) + '\n')
assert not Path('/tmp/.X191-lock').exists()
assert not Path('/tmp/.X11-unix/X191').exists()
runtime = Path('/home/ts/.local/bin/toad').resolve().parent.parent
package = Path('/home/ts/.local/share/agent-comms/native-current-21e71cdc648ed997/node_modules/@earendil-works/pi-coding-agent')
extension = package / 'agent-comms-extensions/global-agent-comms/index.mjs'
env = dict(os.environ, DISPLAY=':191', PI_COMPACTION_TEST_PACKAGE=str(package),
           PYTHONPATH=str(checkout / 'tests'))
# The new private root has no caller from the parent agent's live registry.
# NativeBackendFixture binds its own new owner and native child identity.
for key in ('PI_AGENT_ID', 'AGENT_COMMS_THREAD', 'AGENT_COMMS_SELECTED_TOOL_SOCKET',
            'AGENT_COMMS_SELECTED_TOOL_TOKEN'):
    env.pop(key, None)
cmd = [str(runtime / 'bin/python'), str(checkout / 'tests/goal_generation_installed_journey.py'),
       '--stage', str(root / 'physical-final-20261013-191'), '--extension', str(extension), '--physical']
(root / 'launch.json').write_text(json.dumps({'runtime': str(runtime), 'command': cmd,
    'native_package': str(package)}, indent=2) + '\n')
with (root / 'xvfb.log').open('w') as xlog, (root / 'st.log').open('w') as slog:
    display = subprocess.Popen(['Xvfb', ':191', '-screen', '0', '1600x1050x24', '-nolisten', 'tcp'], stdout=xlog, stderr=xlog)
    terminal = None
    try:
        deadline = time.monotonic() + 5
        while not Path('/tmp/.X11-unix/X191').exists():
            assert display.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.05)
        terminal = subprocess.Popen(['st', '-g', '132x34', '-f', 'monospace:pixelsize=16',
            '-t', 'Owned #777 physical acceptance', '-e', 'script', '-q', '-e',
            '-c', shlex.join(cmd), str(root / 'terminal.log')], env=env, stdout=slog, stderr=slog)
        code = terminal.wait(timeout=100)
        (root / 'process-result.json').write_text(json.dumps({'terminal_exit': code,
            'display_pid': display.pid, 'terminal_pid': terminal.pid}) + '\n')
        assert code == 0, f'Check failed, preserve {root}/terminal.log; no replay'
        receipt = root / 'physical-final-20261013-191/receipt.json'
        assert receipt.is_file(), f'No acceptance receipt: {root}'
        print(receipt.read_text())
    finally:
        if terminal is not None and terminal.poll() is None:
            terminal.terminate()
            terminal.wait(timeout=5)
        display.terminate()
        display.wait(timeout=5)
        (root / 'retired.json').write_text(json.dumps({'display_retired': display.poll() is not None,
            'terminal_retired': terminal is None or terminal.poll() is not None}) + '\n')
