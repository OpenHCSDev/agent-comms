"""Physical Toad on an isolated X display while a real import holds the wire."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from threading import Event

from agent_comms.comms import Comms
from agent_comms.thread_status import StoppedThreadStatus


def capture(display, destination):
    subprocess.run(['import', '-display', display, '-window', 'root', str(destination)], check=True)
    print(f'physical screen: {destination}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / 'wire'
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    for index in range(85):
        comms.registry.register(comms.registry.new_thread(f'sentinel-{index:02}', frozenset({'proof'}), str(directory)),
                                StoppedThreadStatus())
    comms.messaging.user_identity(str(directory))
    source = directory / 'source.jsonl'
    source.write_text('\n'.join(json.dumps({
        'type': kind, 'uuid': str(index), 'timestamp': '2026-10-19T00:00:00Z',
        'cwd': str(directory), 'sessionId': 'proof', 'promptId': 'p-1', 'origin': {'kind': 'human'},
        'message': {'content': text if kind == 'user' else [{'type': 'text', 'text': text}]},
    }) for index, (kind, text) in enumerate((('user', 'Scratch input'), ('assistant', 'Scratch reply')))) + '\n')
    environment = {key: value for key, value in os.environ.items() if not key.startswith('PI_')
                   and key not in ('AGENT_COMMS_MANAGED', 'AGENT_COMMS_THREAD')}
    environment.update(AGENT_COMMS_ROOT=str(root), XDG_STATE_HOME=str(directory / 'state'),
                       XDG_CONFIG_HOME=str(directory / 'config'))
    processes = []
    try:
        x = subprocess.Popen(['Xvfb', '-displayfd', '1', '-screen', '0', '1500x1000x24', '-nolisten', 'tcp'],
                             stdout=subprocess.PIPE, stderr=directory.joinpath('xvfb.log').open('w'), text=True)
        processes.append(x)
        display = ':' + x.stdout.readline().strip()
        environment['DISPLAY'] = display
        importer = subprocess.Popen([sys.executable, str(Path(sys.executable).with_name('agent-comms')), '--root', str(root),
                                     'import-thread', '--harness', 'claude_code', '--source', str(source),
                                     '--name', 'imported', '--worktree', str(directory)], env=environment,
                                    stdout=directory.joinpath('import.stdout').open('w'),
                                    stderr=directory.joinpath('import.stderr').open('w'), start_new_session=True)
        processes.append(importer)
        ui = subprocess.Popen(['st', '-g', '160x50', '-e', sys.executable, '-m', 'toad', 'acp',
                               str(Path(sys.executable).with_name('agent-comms-acp')), str(directory)],
                              env=environment, stdout=directory.joinpath('toad.stdout').open('w'),
                              stderr=directory.joinpath('toad.stderr').open('w'), start_new_session=True)
        processes.append(ui)
        started = time.monotonic()
        Event().wait(15)
        print(f't={time.monotonic()-started:.3f}s importer_exit={importer.poll()} registry={len(comms.registry.snapshot().threads)} ui_exit={ui.poll()}', flush=True)
        capture(display, directory / 'during-import.png')
        if importer.poll() is None:
            os.killpg(importer.pid, signal.SIGTERM)
            importer.wait(timeout=5)
        Event().wait(10)
        print(f't={time.monotonic()-started:.3f}s importer_exit={importer.poll()} registry={len(comms.registry.snapshot().threads)} ui_exit={ui.poll()}', flush=True)
        capture(display, directory / 'after-import.png')
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        comms.views.transcript_reads.close()


if __name__ == '__main__':
    main()
