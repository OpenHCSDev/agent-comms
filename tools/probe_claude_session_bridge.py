"""Real Claude SessionStart -> Monitor -> /compact -> reader loss -> reattachment.

Uses a new private bus/session, installed entrypoints with the selected source,
real hooks, the configured Claude provider, and retained stdout/transcript proof.
No live configuration or installation is changed. Never retries an input.
"""
from __future__ import annotations

import argparse
from contextlib import closing, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    run.mkdir(parents=True, exist_ok=False)
    source = args.source.resolve()
    sys.path.insert(0, str(source / 'src'))
    from agent_comms.cli import main as cli_main
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.cli_bridges.claude import RunningClaude
    from agent_comms.cli_bridges.shared import DeliveryRow, MonitorObservation, MonitorPart, ObservedPart
    from agent_comms.cli_protocol import DeliveredOutcome
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms_hooks.watch import WatchClaim

    root, state = run / 'bus', run / 'bridge'
    session = str(uuid4())
    claim = WatchClaim(session)
    metadata = {'owner': 'pi-bridge-compact', 'purpose': __doc__, 'session': session,
                'source': str(source), 'root': str(root), 'state': str(state)}
    (run / 'owner.json').write_text(json.dumps(metadata, indent=2))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('CLAUDE_', 'AGENT_COMMS_', 'PI_')) and k != 'CLAUDECODE'}
    env.update(AGENT_COMMS_ROOT=str(root), PYTHONPATH=str(source / 'src'))
    bin_dir = Path(sys.executable).parent

    def cli(*words):
        output = StringIO()
        with redirect_stdout(output):
            code = cli_main(['--root', str(root), *words])
        if code:
            raise RuntimeError(output.getvalue())
        return json.loads(output.getvalue())

    for name in ('session-proof', 'proof-sender'):
        cli('register', '--name', name, '--worktree', str(run), '--execution', 'external')
    bridge_command = [str(bin_dir / 'agent-comms-claude-bridge'), 'enrol', '--state', str(state),
                      '--root', str(root), '--bus-thread', 'session-proof', '--session', session]
    subprocess.run(bridge_command, env=env, check=True)
    watch = f'{bridge_command[0]} watch --state {state}'
    hook = run / 'hook.py'
    hook.write_text('import io,json,os,sys,time\nfrom pathlib import Path\n'
        'r=json.load(sys.stdin)\n'
        f'with Path({str(run / "hooks.jsonl")!r}).open("a") as f: '
        'f.write(json.dumps({"at":time.time(),"hook":r,"environment":{k:v for k,v in os.environ.items() '
        'if k in ("CLAUDE_PID","CLAUDE_CODE_SESSION_ID")}})+"\\n")\n'
        'sys.stdin=io.StringIO(json.dumps(r))\n'
        'from agent_comms_hooks.session import started,ended\n'
        'from agent_comms_hooks.compaction import starting,finished\n'
        'if r["hook_event_name"]=="SessionStart":\n'
        ' started()\n'
        ' if r.get("source")=="compact":\n'
        '  sys.stdin=io.StringIO(json.dumps(r))\n'
        '  finished()\n'
        'elif r["hook_event_name"]=="SessionEnd": ended()\n'
        'elif r["hook_event_name"]=="PreCompact": starting()\n')
    settings = run / 'settings.json'
    settings.write_text(json.dumps({'hooks': {event: [{'hooks': [{'type': 'command',
        'command': f'{sys.executable} {hook}'}]}]
        for event in ('SessionStart', 'SessionEnd', 'PreCompact')}}))
    command = ['claude', '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
        '--verbose', '--settings', str(settings), '--setting-sources', '', '--session-id', session,
        '--permission-mode', 'bypassPermissions', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--tools', 'Monitor,Bash', '--system-prompt',
        'Follow the user literally. Never reattach a Monitor until explicitly asked. '
        'On its termination event reply STOPPED; do not run tools. On a comms event reply RECEIVED.']
    replies = queue.Queue()
    with (run / 'claude.jsonl').open('w') as log, (run / 'stderr.log').open('w') as errors, \
            (run / 'statuses.jsonl').open('w') as statuses, (run / 'events.jsonl').open('w') as events:
        def event(kind, **data):
            record = {'at': time.time(), 'event': kind, **data}
            events.write(json.dumps(record) + '\n'); events.flush()
            print(json.dumps(record), flush=True)

        child = subprocess.Popen(command, env=env, cwd=run, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
        def read():
            for line in child.stdout:
                log.write(line); log.flush()
                result = json.loads(line)
                if result.get('type') == 'result':
                    replies.put(result)
            replies.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def send(text):
            child.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': text}}) + '\n')
            child.stdin.flush()
            event('input', text=text)
        def hooks():
            path = run / 'hooks.jsonl'
            return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
        def deliveries(message_id):
            with closing(CoordinationStore.read_snapshot(state / 'delivery.sqlite3')) as db:
                return DeliveryRow.select(db, id=message_id), ObservedPart.select(db, id=message_id)
        def status(second, phase):
            result = cli('threads')
            record = {'at': time.time(), 'second': second, 'phase': phase, **result}
            statuses.write(json.dumps(record) + '\n'); statuses.flush()
            print(json.dumps(record), flush=True)
            return next(row for row in result['threads'] if row['name'] == 'session-proof')

        seed = ('Use Monitor to run exactly this command with timeout_ms=1800000, then reply WATCHING. '
            'Never re-arm after reader loss until my explicit REATTACH instruction.\n' + watch +
            '\nKeep these rules during compaction:\n' + '\n'.join(
            f'{i}. Preserve uncertain attempts; the session owns presence and Monitor only reads events.'
            for i in range(80)))
        phase, first_result, compact_done = 'first', False, False
        message_id = None
        killed_at = delivered_at = None
        daemon = None
        try:
            send(seed)
            start = time.monotonic()
            for second in range(240):
                due = start + second - time.monotonic()
                if due > 0:
                    time.sleep(due)
                while True:
                    try:
                        result = replies.get_nowait()
                    except queue.Empty:
                        break
                    event('claude-result', result=result)
                    if result is None:
                        raise RuntimeError('Claude exited; no input replay')
                    if phase == 'first':
                        first_result = True
                    if result.get('local_command') == 'compact':
                        compact_done = True
                row = status(second, phase)
                service_log = claim.path.with_suffix('.log').read_text() if claim.path.with_suffix('.log').exists() else ''
                if phase == 'first' and first_result and 'Fresh Monitor probe observed' in service_log:
                    daemon = ProcessIdentity.capture(row['process_identity']['pid'])
                    event('initial-reader-ready', bridge=daemon.pid)
                    send('/compact')
                    phase = 'compacting'
                elif phase == 'compacting' and any(h['hook']['hook_event_name'] == 'PreCompact' for h in hooks()):
                    readers = []
                    for proc in Path('/proc').iterdir():
                        if not proc.name.isdecimal():
                            continue
                        try:
                            argv = (proc / 'cmdline').read_bytes().split(b'\0')
                            if b'watch' in argv and str(state).encode() in argv:
                                readers.append(ProcessIdentity.capture(int(proc.name)))
                        except (FileNotFoundError, ProcessLookupError, PermissionError):
                            continue
                    if len(readers) != 1:
                        raise RuntimeError(f'Expected exactly one Monitor reader, got {readers}')
                    readers[0].signal(signal.SIGTERM)
                    killed_at = second
                    event('reader-killed-during-compaction', reader=readers[0].pid, bridge=daemon.pid)
                    phase = 'paused'
                elif phase == 'paused':
                    assert row['process_identity']['pid'] == daemon.pid and row['status'] in ('connected', 'compacting'), row
                    if second >= killed_at + 2 and message_id is None:
                        sent = cli('send', '--from', 'proof-sender', '--to', 'session-proof',
                                   '--body', 'SESSION-LIFETIME-EXACTLY-ONCE-' + session)
                        event('message-sent-while-unattached', result=sent)
                        message_id = sent['id']
                    if message_id is not None:
                        rows, parts = deliveries(message_id)
                        assert not rows and not parts, (rows, parts)
                    if message_id is not None and compact_done and second >= killed_at + 8:
                        event('paused-without-attempt', message_id=message_id, seconds=second-killed_at)
                        send('REATTACH: use Monitor with timeout_ms=1800000 for exactly this command, '
                             'then reply REATTACHED. Do not otherwise run tools.\n' + watch)
                        phase = 'replacement'
                elif phase == 'replacement':
                    assert row['process_identity']['pid'] == daemon.pid and row['status'] == 'connected', row
                    rows, parts = deliveries(message_id)
                    if rows and rows[0].outcome is DeliveredOutcome:
                        assert len(parts) == 1, parts
                        event('delivery-confirmed', message_id=message_id,
                              record_id=parts[0].record_id, body=parts[0].body)
                        delivered_at = second
                        phase = 'delivered'
                elif phase == 'delivered' and second >= delivered_at + 8:
                    history = RunningClaude(session)
                    with history.source.open('rb') as stream:
                        observations = [u for u in history.decoded(stream, 0, history.source.stat().st_size, history.start_state())
                            if isinstance(u, MonitorObservation) and isinstance(u.frame, MonitorPart)
                            and u.frame.id == message_id]
                    assert len(observations) == 1, observations
                    event('exactly-once-in-real-transcript', message_id=message_id, count=len(observations))
                    break
            else:
                raise TimeoutError('Proof incomplete; no input replay')
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.terminate(); child.wait(timeout=10)
            reader.join(timeout=5)
            for second in range(5):
                row = status(second, 'ended')
                if daemon is None or not daemon.alive():
                    event('session-ended', bridge_alive=False, hooks=hooks())
                    break
                time.sleep(1)

        # Resume the completed scratch session. Kill only after its new answer
        # finishes, so this crash check does not manufacture an uncertain input.
        from agent_comms.cli_bridges.monitor import EndSession, VerifySession
        while not replies.empty():
            replies.get_nowait()
        resumed = list(command)
        resumed[resumed.index('--session-id')] = '--resume'
        child = subprocess.Popen(resumed, env=env, cwd=run, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        replacement = None
        try:
            send('Reply exactly RESUMED. Do not call tools or attach a Monitor.')
            answered = False
            start = time.monotonic()
            for second in range(90):
                due = start + second - time.monotonic()
                if due > 0:
                    time.sleep(due)
                row = status(second, 'resume')
                while not replies.empty():
                    result = replies.get_nowait()
                    event('resume-result', result=result)
                    if result is None or result.get('is_error'):
                        raise RuntimeError('Resume failed; no input replay')
                    answered = True
                if answered and row['status'] == 'connected':
                    replacement = ProcessIdentity.capture(row['process_identity']['pid'])
                    assert replacement != daemon
                    owner = ProcessIdentity.capture(child.pid)
                    VerifySession(owner=owner).exchange(claim)
                    stale = ProcessIdentity(owner.pid, owner.start_time + 1)
                    try:
                        EndSession(owner=stale).exchange(claim)
                    except ValueError:
                        event('stale-session-end-refused', bridge=replacement.pid)
                    else:
                        raise AssertionError('Stale SessionEnd was accepted')
                    event('resumed-session-ready', bridge=replacement.pid, owner=owner.pid)
                    owner.signal(signal.SIGKILL)
                    child.wait(timeout=10)
                    reader.join(timeout=5)
                    break
            else:
                raise TimeoutError('Resume unfinished; no input replay')
            for second in range(10):
                row = status(second, 'claude-killed')
                if row['status'] == 'no_bridge' and not replacement.alive():
                    event('claude-exit-released-registration', bridge=replacement.pid)
                    break
                time.sleep(1)
            else:
                raise AssertionError('Claude exited but its bridge remained registered')
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            reader.join(timeout=5)


if __name__ == '__main__':
    main()
