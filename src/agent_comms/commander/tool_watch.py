"""Follow every live native session file and log each tool call with its thread name.

Writes one line per call to activity.log. At each turn end (or every 25 calls in a
long turn) prints one digest of what the unit did, for the commander to judge by reading it.
"""
import json, os, time, glob, collections
from pathlib import Path

from . import order
from .config import CommanderConfig
from ..field_codec import MalformedRecord
from ..native_tool_call import SelectedToolDenied
from ..native_tools import CodingTool
from ..tool_declaration import ToolDeclaration, one_line
from ..registration import Registration

CONFIG = CommanderConfig.installed()
B = str(CONFIG.root)
LOG = f'{CONFIG.state}/activity.log'


def thread_names():
    """Session file -> thread name, from the registry's session_file (read-only)."""
    with open(f'{B}/registry.json') as f:
        threads = json.load(f)['threads']
    return {t['session_file']: name for name, t in threads.items() if t.get('session_file')}


def decoded(name, args):
    """Core's argument record of the call; None when Core declares none or refuses it."""
    try:
        return CodingTool.from_call(name, args, call_id='tool_watch').tool
    except (SelectedToolDenied, MalformedRecord):
        return None


def digest_line(name, args, tool):
    """What the call does, in enough detail to judge it."""
    return ToolDeclaration.call_digest(name, tool, args)


STRUCK = {}  # (thread, violation) -> time of the strike, so one repeated command is one strike


def enforce_storage(registry, thread, tool):
    """Core's StorageRule decides the decoded call; a denial is a public strike on the unit's
    current task, at once."""
    if tool is None or thread in CONFIG.storage_exempt or thread not in registry:
        return  # Core decides nothing for a call or unit it does not know
    try:
        worktree = Path(registry.require(thread).worktree)
        registry.decide_storage(thread, tool.storage_effects(worktree))
        return
    except SelectedToolDenied as denied:
        violation = str(denied)
    if time.time() - STRUCK.get((thread, violation), 0) < 600:
        return
    STRUCK[(thread, violation)] = time.time()
    print(f'VIOLATION storage {thread}: {violation}', flush=True)
    if task := next((t.id for t in order.held_by(thread)), None):
        order.strike(thread, f'{task} broke the storage rule at a tool call: {violation}; '
                             'storage waste is banned outright', 'tool_watch at tool call')


def emit(turns, thread, final=''):
    calls = turns.pop(thread, [])
    if not calls and (not final or final.strip() == '{"decision":"IGNORE"}'):
        return
    lines = [f'TURN {thread} ({len(calls)} calls)']
    lines += [f'  {c}' for c in calls[-14:]]
    if final:
        lines.append(f'  says: {one_line(final, 400)}')
    print('\n'.join(lines)[:3000], flush=True)


def follow(registry, turns, thread, lines):
    """Each assistant tool call in the session lines is decoded once, logged, digested, journaled
    and judged for storage."""
    for line in lines:
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
            tool = decoded(name, args)
            digest = digest_line(name, args, tool)
            with open(LOG, 'a') as log:
                log.write(f"{time.strftime('%H:%M:%S')} {thread} {digest[:260]}\n")
            turns[thread].append(digest)
            enforce_storage(registry, thread, tool)
            if len(turns[thread]) >= 25:
                emit(turns, thread)
        if msg.get('stopReason') == 'stop':
            emit(turns, thread, text)


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
            follow(registry, turns, thread, data[:cut].splitlines())
        time.sleep(2)
