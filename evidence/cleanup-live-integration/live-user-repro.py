"""Operator capture of the default installed app on an authorized real fork.

No provider substitution or desktop input. Reuse the recorder's process custody.
Retain the fork journal and uncertain input if the actual backend fails.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

recorder = Path('/home/ts/wt/toad-viewport-demand-20260929/tests/tools/record_installed_tui.py')
spec = importlib.util.spec_from_file_location('existing_recorder', recorder)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from agent_comms.registration import Registration

arguments = argparse.ArgumentParser()
arguments.add_argument('--output', type=Path, required=True)
arguments.add_argument('--thread', default='comms-live-repro-20260929')
arguments.add_argument('--send-check', action='store_true')
args = arguments.parse_args()
root = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
output = args.output
output.mkdir(parents=True, exist_ok=False, mode=0o700)
owner = module.ProcessOwner(Registration(root / 'registry.json'))
env = {k: v for k, v in os.environ.items() if not k.startswith(('AGENT_COMMS_', 'PI_')) and k not in ('PYTHONPATH', 'NO_COLOR')}
events = []
started = time.monotonic()
def mark(label, **fields):
    if 'terminal' in globals():
        fields['kernel_cpu'] = module.cpu_snapshot(terminal.child.identity.pid)
    event = dict(label=label, seconds=time.monotonic()-started, **fields)
    events.append(event)
    (output / 'events.json').write_text(json.dumps(events, indent=2))
    print(json.dumps(event), flush=True)

def xdo(*args):
    return owner.run(['/usr/bin/xdotool', *args], env, stdout=subprocess.PIPE, timeout=10).stdout.decode().strip()

try:
    read_fd, write_fd = os.pipe()
    try:
        display = owner.start(['/usr/bin/Xvfb', '-displayfd', str(write_fd), '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                              env=env, pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(write_fd)
        write_fd = None
        with os.fdopen(read_fd) as stream:
            env['DISPLAY'] = ':' + stream.readline().strip()
    finally:
        if write_fd is not None:
            os.close(write_fd)
    mark('isolated-display', display=env['DISPLAY'])
    terminal = owner.start(['/usr/local/bin/st', '-g', '140x45', '-t', 'comms-live-repro', '-e',
                            '/home/ts/.agent-comms/stack/bin/toad-comms', args.thread],
                           env=env, stdout=subprocess.DEVNULL, stderr=(output / 'terminal.log').open('wb'))
    window = xdo('search', '--sync', '--name', '^comms-live-repro$').splitlines()[0]
    xdo('windowsize', window, '1280', '900')
    xdo('windowfocus', window)
    ui = module.terminal_program(owner, terminal.child.identity, time.monotonic()+20)
    mark('actual-default-ui', pid=ui.child.identity.pid, start_ticks=ui.child.identity.start_time)
    profile = owner.start(['sudo', '-n', '/home/ts/.local/bin/py-spy', 'record', '--pid', str(ui.child.identity.pid),
                           '--duration', '42', '--rate', '50', '--gil', '--format', 'speedscope',
                           '--output', str(output / 'actual-ui.speedscope.json')],
                          env=env, stdout=(output / 'profiler.log').open('wb'), stderr=subprocess.STDOUT)
    video = owner.start(['/usr/bin/ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'x11grab',
                         '-framerate', '15', '-video_size', '1280x900', '-i', env['DISPLAY'],
                         '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '25', '-t', '100', str(output / 'capture.mp4')],
                        env=env, stdout=subprocess.DEVNULL, stderr=(output / 'video.log').open('wb'))
    mark('recording-started')
    time.sleep(12)
    owner.run(['/usr/bin/import', '-display', env['DISPLAY'], '-window', 'root', str(output / 'before.png')], env)
    if args.send_check:
        prompt = 'Second bounded live connectivity check only. Reply exactly LIVE_PATH_SECOND_OK_20260929. Do not use tools, send messages, resume any earlier task or goal, or change files.'
        xdo('type', '--clearmodifiers', '--delay', '1', prompt)
        mark('typed-original-input', text=prompt)
        xdo('key', '--clearmodifiers', 'Return')
        mark('submitted-original-input')
    time.sleep(8)
    # The physical window is isolated; focus the actual message viewport.
    xdo('mousemove', '--window', window, '760', '350', 'click', '1')
    for key, label in [('Prior', 'page-up'), ('Next', 'page-down'), ('Prior', 'reverse-page-up')]:
        mark(label)
        xdo('keydown', key)
        time.sleep(4)
        xdo('keyup', key)
    mark('end-destination')
    xdo('key', 'End')
    mark('stationary')
    time.sleep(15)
    for name in ('screen',):
        owner.run([sys.executable, '/home/ts/wt/toad-viewport-demand-20260929/tools/performance/capture_live.py',
                   '--pid', str(ui.child.identity.pid), '--output-dir', str(output), '--name', 'after-'+name, '--'+name, '--sudo'],
                  env, stdout=subprocess.PIPE, timeout=30)
    mark('capture-complete')
    owner.run(['/usr/bin/import', '-display', env['DISPLAY'], '-window', 'root', str(output / 'after.png')], env)
finally:
    cleanup = owner.cleanup()
    (output / 'cleanup.json').write_text(json.dumps(cleanup, indent=2))
    mark('own-ui-cleanup', remaining_owned_pids=cleanup.get('remaining_owned_pids'), errors=cleanup.get('errors'))
    # Durable fork ownership is deliberately excluded from recorder teardown.
    # Inspect and retire it through OwnerLifecycle only after its turn settles.
