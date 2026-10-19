"""Real continuous Claude Monitor -> /compact, on a new private bus/session.

The parent logs Claude's actual streaming output; nothing injects hook payloads.
Use LABEL SOURCE. Scratch ownership is recorded in docs/bridge-compaction.md.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import argparse
from uuid import uuid4

PYTHON = Path('/home/ts/code/projects/agent-comms/stack/.venv/bin/python')
BIN = PYTHON.parent
SCRATCH = Path('/home/ts/.cache/agent-scratch/pi-bridge-compact')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    parser.add_argument('source', type=Path)
    parser.add_argument('--compaction-hooks', action='store_true')
    parser.add_argument('--claude', default='claude', help='Pin the reporting session executable when reproducing')
    options = parser.parse_args()
    source = options.source.resolve()
    # Warm the actual CLI entrypoint once. Spawning a fresh interpreter every
    # second costs more than a second on this host and distorts the observation.
    sys.path.insert(0, str(source / 'src'))
    from agent_comms.cli import main as cli_main

    run = SCRATCH / options.label
    run.mkdir(parents=True, exist_ok=False)
    root = run / 'bus'
    session = str(uuid4())
    (run / 'session').write_text(session)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('CLAUDE_', 'AGENT_COMMS_', 'PI_')) and key != 'CLAUDECODE'}
    env.update(AGENT_COMMS_ROOT=str(root), PYTHONPATH=str(Path(source).resolve() / 'src'))
    settings = run / 'settings.json'
    logger = run / 'hook.py'
    logger.write_text('import io,json,sys,time\nfrom pathlib import Path\n'
                      f'p=Path({str(run / "hooks.jsonl")!r})\n'
                      'record=json.load(sys.stdin)\n'
                      'with p.open("a") as f: f.write(json.dumps({"at":time.time(),"hook":record})+"\\n")\n'
                      + ('sys.stdin=io.StringIO(json.dumps(record))\n'
                         'from agent_comms_hooks.compaction import starting,finished\n'
                         'if record["hook_event_name"] == "PreCompact": starting()\n'
                         'elif record.get("source") == "compact": finished()\n'
                         if options.compaction_hooks else ''))
    settings.write_text(json.dumps({'hooks': {event: [{'hooks': [{'type': 'command',
        'command': f'{PYTHON} {logger}'}]}] for event in ('PreCompact', 'SessionStart')}}))
    subprocess.run([str(BIN / 'agent-comms'), '--root', str(root), 'register', '--name',
        'compact-proof', '--worktree', str(run)], env=env, check=True, capture_output=True)
    with redirect_stdout(StringIO()):
        if cli_main(['--root', str(root), 'threads']):
            raise RuntimeError('CLI warm-up failed')
    command = [options.claude, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
        '--verbose', '--settings', str(settings), '--setting-sources', '', '--session-id', session,
        '--permission-mode', 'bypassPermissions', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--tools', 'Monitor,Bash',
        '--system-prompt', 'You are a verification assistant. Follow the user literally.']
    replies = queue.Queue()
    with (run / 'claude.jsonl').open('w') as log, (run / 'stderr.log').open('w') as errors:
        child = subprocess.Popen(command, env=env, cwd=run, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
        def read():
            for line in child.stdout:
                log.write(line)
                log.flush()
                event = json.loads(line)
                if event.get('type') == 'result':
                    replies.put(event)
            replies.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def send(text):
            child.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': text}}) + '\n')
            child.stdin.flush()
        bridge_command = (f'{BIN / "agent-comms-claude-bridge"} watch --state {run / "bridge"} '
            f'--root {root} --bus-thread compact-proof --session {session} 2>&1')
        seed = ('Use the Monitor tool to run exactly this command with timeout_ms=1800000. '
                'Do not use Bash for the watch and do not start a second watch. Then reply only WATCHING.\n'
                + bridge_command + '\nKeep these constraints in context:\n' + '\n'.join(
                f'{i}. Preserve original sessions, auth, uncertain attempts and messages. '
                'Only the connected bridge owns presence; a UI displays canonical state.' for i in range(100)))
        try:
            send(seed)
            first = replies.get(timeout=150)
            print('first result:', first, flush=True)
            if first is None:
                raise RuntimeError('Claude exited before the watch was established')
            with (run / 'statuses.jsonl').open('w') as status:
                start = time.monotonic()
                sent_at = finished_at = None
                for second in range(120):
                    due = start + second - time.monotonic()
                    if due > 0:
                        time.sleep(due)
                    view = StringIO()
                    with redirect_stdout(view):
                        code = cli_main(['--root', str(root), 'threads'])
                    if code:
                        raise RuntimeError(view.getvalue())
                    observed = json.loads(view.getvalue())
                    row = {'second': second, 'at': time.time(), 'threads': observed}
                    status.write(json.dumps(row) + '\n')
                    status.flush()
                    print(json.dumps(row), flush=True)
                    # Wait for actual registration, not Claude's WATCHING text.
                    if sent_at is None and second >= 4 and observed['threads'][0]['status'] == 'connected':
                        send('/compact')
                        sent_at = second
                    if sent_at is not None and finished_at is None:
                        try:
                            result = replies.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            if result is None or result.get('local_command') == 'compact':
                                print('compact result:', result, flush=True)
                                finished_at = second
                    if finished_at is not None and second >= finished_at + 5:
                        break
                else:
                    raise TimeoutError('Original compaction unfinished; never replay it')
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)
            reader.join(timeout=5)


if __name__ == '__main__':
    main()
