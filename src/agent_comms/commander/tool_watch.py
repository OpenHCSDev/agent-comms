"""Follow every live native session file and log each tool call with its thread name.

Writes one line per call to activity.log. At each turn end (or every 25 calls in a
long turn) prints one digest of what the unit did, for the commander to judge by reading it.
"""
import json, os, time, glob, collections
from pathlib import Path

from . import order
from .config import CommanderConfig
from ..native_tool_call import SelectedToolDenied
from ..native_tools import NativeExecutableTool
from ..registration import Registration

CONFIG = CommanderConfig.installed()
B = str(CONFIG.root)
LOG = f'{CONFIG.state}/activity.log'
EDITS = f'{CONFIG.state}/edits.jsonl'  # every edit in full; judge owns verdicts
WORKTREES = {}  # thread -> cwd, from process environments


def thread_names():
    """Session file -> thread name, from the registry's session_file (read-only)."""
    with open(f'{B}/registry.json') as f:
        threads = json.load(f)['threads']
    return {t['session_file']: name for name, t in threads.items() if t.get('session_file')}


def summarize(name, args):
    if not isinstance(args, dict):
        return str(args)[:160]
    for key in ('command', 'path', 'file_path', 'pattern', 'query', 'to', 'target'):
        if key in args:
            return f'{key}={str(args[key])[:200]!r}'
    return json.dumps(args)[:160]


def first(text, n):
    return ' '.join(str(text or '').split())[:n]


def digest_line(name, args):
    """What the call does, in enough detail to judge it."""
    if not isinstance(args, dict):
        return f'{name} {first(args, 160)}'
    if name == 'edit':
        edits = args.get('edits') or [args]
        if isinstance(edits, str):
            edits = json.loads(edits)
        edits = [e if isinstance(e, dict) else {'newText': str(e)} for e in edits]
        parts = [f"-{first(e.get('oldText') or e.get('old_string'), 90)} +{first(e.get('newText') or e.get('new_string'), 140)}"
                 for e in edits[:2]]
        return f"edit {args.get('path') or args.get('file_path')}: " + ' | '.join(parts)
    if name == 'write':
        return f"write {args.get('path') or args.get('file_path')}: {first(args.get('content'), 160)}"
    if name == 'bash':
        return f"bash {first(args.get('command'), 260)}"
    if name.startswith('comms_'):
        return f"{name} {first(json.dumps(args), 260)}"
    return f'{name} {summarize(name, args)}'


def record_edit(thread, name, args):
    """Each edit waits for a verdict from the commander (judge); nothing judges it automatically."""
    if name not in ('edit', 'write') or not isinstance(args, dict):
        return
    edits = args.get('edits') or [args]
    if isinstance(edits, str):
        edits = json.loads(edits)
    hunks = [dict(old=e.get('oldText') or e.get('old_string') or '', new=e.get('newText') or e.get('new_string') or e.get('content') or '')
             for e in edits if isinstance(e, dict)]
    record = dict(id=f'{time.time_ns():x}'[-10:], at=time.time(), thread=thread,
                  path=args.get('path') or args.get('file_path'), hunks=hunks)
    with open(EDITS, 'a') as f:
        f.write(json.dumps(record) + '\n')


STRUCK = {}  # (thread, violation) -> time of the strike, so one repeated command is one strike


def enforce_storage(registry, thread, name, args):
    """Core decodes the call and its StorageRule decides it; a denial is a public strike on the
    unit's current task, at once."""
    if not isinstance(args, dict) or thread in CONFIG.storage_exempt:
        return
    if thread not in registry or name not in NativeExecutableTool.names():
        return  # Core knows neither this unit nor this tool, so it decides nothing
    try:
        call = NativeExecutableTool.from_call(name, args, call_id='tool_watch')
    except SelectedToolDenied:
        return  # Core refuses to decode the call, so it ran nothing
    try:
        worktree = Path(registry.require(thread).worktree)
        registry.decide_storage(thread, call.tool.storage_effects(worktree))
        return
    except SelectedToolDenied as denied:
        violation = str(denied)
    if time.time() - STRUCK.get((thread, violation), 0) < 600:
        return
    STRUCK[(thread, violation)] = time.time()
    print(f'VIOLATION storage {thread}: {violation}', flush=True)
    if task := next((t.id for t in order.open_todos(thread)), None):
        order.strike(thread, f'{task} broke the storage rule at a tool call: {violation}; '
                             'storage waste is banned outright', 'tool_watch at tool call')


def emit(turns, thread, final=''):
    calls = turns.pop(thread, [])
    if not calls and (not final or final.strip() == '{"decision":"IGNORE"}'):
        return
    lines = [f'TURN {thread} ({len(calls)} calls)']
    lines += [f'  {c}' for c in calls[-14:]]
    if final:
        lines.append(f'  says: {first(final, 400)}')
    print('\n'.join(lines)[:3000], flush=True)


def main(a):
    names, offsets = thread_names(), {}
    registry = Registration(Path(B) / 'registry.json')
    turns = collections.defaultdict(list)
    last_names = time.time()
    while True:
        if time.time() - last_names > 60:
            names.update(thread_names()); last_names = time.time()
        for path in set(glob.glob(f'{B}/native-sessions/*/*.jsonl')) | set(names):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if time.time() - st.st_mtime > 900 and path not in offsets:
                continue
            start = offsets.get(path, st.st_size)  # new files: start at end; only new calls matter
            if st.st_size <= start:
                offsets[path] = st.st_size if st.st_size < start else start
                continue
            with open(path, 'rb') as f:
                f.seek(start); data = f.read(st.st_size - start)
            cut = data.rfind(b'\n') + 1
            offsets[path] = start + cut
            if path not in names and time.time() - last_names > 10:
                names.update(thread_names()); last_names = time.time()
            thread = names.get(path, path.split('/')[-2][:8])
            for line in data[:cut].splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get('message') or {}
                if msg.get('role') != 'assistant':
                    continue
                text = ''
                for block in msg.get('content') or ():
                    if block.get('type') == 'text':
                        text += block.get('text') or ''
                    if block.get('type') != 'toolCall':
                        continue
                    name, args = block.get('name'), block.get('arguments')
                    with open(LOG, 'a') as log:
                        log.write(f"{time.strftime('%H:%M:%S')} {thread} {name} {summarize(name, args)}\n")
                    turns[thread].append(digest_line(name, args))
                    record_edit(thread, name, args)
                    enforce_storage(registry, thread, name, args)
                    if len(turns[thread]) >= 25:
                        emit(turns, thread)
                if msg.get('stopReason') == 'stop':
                    emit(turns, thread, text)
        time.sleep(2)
