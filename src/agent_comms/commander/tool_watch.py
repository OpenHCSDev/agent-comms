"""Follow every live native session file and log each tool call with its thread name.

Writes one line per call to activity.log. At each turn end (or every 25 calls in a
long turn) prints one digest of what the unit did, for the commander to judge by reading it.
"""
import os, time, glob, collections
from pathlib import Path

from . import order
from .config import CommanderConfig
from ..channel_coding_tools import CodingCall, RefusedCodingCall
from ..field_codec import MalformedRecord
from ..native_entries import NativeEntry
from ..native_tool_call import SelectedToolDenied
from ..native_tools import CodingTool
from ..pi_payloads import PiMessage, TextContent
from ..tool_declaration import ToolDeclaration, one_line
from ..registration import Registration
from ..todos import StorageStrikeTodo

CONFIG = CommanderConfig.installed()
B = str(CONFIG.root)
LOG = f'{CONFIG.state}/activity.log'


def thread_names(registry: Registration) -> dict[str, str]:
    """Session file -> thread name, from the registry snapshot's threads."""
    return {thread.session_file: name for name, thread in registry.snapshot().threads.items()
            if thread.session_file}


def decoded(name: str, args: object, call_id: str) -> CodingCall:
    """The recorded call owns either its decoded tool or its argument refusal."""
    try:
        return CodingTool.from_call(name, args, call_id=call_id)
    except (SelectedToolDenied, MalformedRecord) as refused:
        print(f'REFUSED tool {name} [{call_id}]: {one_line(str(refused), 200)}', flush=True)
        return RefusedCodingCall(call_id, name, args, str(refused))


def digest_line(name: str, args: object, call: CodingCall) -> str:
    """What the call does, in enough detail to judge it."""
    try:
        tool = call.require_tool()
    except SelectedToolDenied:
        return f'REFUSED {name}: {one_line(str(args), 200)}'
    return ToolDeclaration.call_digest(name, tool, args)


def enforce_storage(registry: Registration, thread: str, call: CodingCall, record: str) -> None:
    """Core judges its units and records each storage violation once, citing the session
    line ``record`` that made the call."""
    if thread not in order.units() or thread not in registry:
        return
    try:
        tool = call.require_tool()
    except SelectedToolDenied:
        return
    try:
        worktree = Path(registry.require(thread).worktree)
        registry.decide_storage(thread, tool.storage_effects(worktree))
        return
    except SelectedToolDenied as denied:
        violation = str(denied)
    print(f'VIOLATION storage {thread}: {violation}', flush=True)
    for todo in order.held_by(thread)[:1]:
        order.strike(thread, todo.id,
                     f'{todo.id} broke the storage rule at the tool call in {record}: {violation}; '
                     'storage waste is banned outright', 'tool_watch at tool call',
                     kind=StorageStrikeTodo)


def emit(turns, thread, final=''):
    calls = turns.pop(thread, [])
    if not calls and (not final or final.strip() == '{"decision":"IGNORE"}'):
        return
    lines = [f'TURN {thread} ({len(calls)} calls)']
    lines += [f'  {c}' for c in calls[-14:]]
    if final:
        lines.append(f'  says: {one_line(final, 400)}')
    print('\n'.join(lines)[:3000], flush=True)


def follow(registry: Registration, turns: dict[str, list[str]], thread: str, path: str,
           start: int, data: bytes) -> None:
    """Each complete session line from byte `start` is decoded once by Core's native entry
    family; each assistant tool call is logged, digested and judged for storage. A line
    Core refuses is reported."""
    offset = start
    for line in data.splitlines(keepends=True):
        at, offset = offset, offset + len(line)
        try:
            entry = NativeEntry.read(line)
        except (ValueError, MalformedRecord) as refused:
            print(f'REFUSED {path}@{at}: {one_line(str(refused), 200)}', flush=True)
            continue
        for message in entry.messages:
            if message.assistant:
                walk(registry, turns, thread, message, f'{os.path.realpath(path)}@{at}')


def walk(registry: Registration, turns: dict[str, list[str]], thread: str,
         message: PiMessage, record: str) -> None:
    for call in message.tool_calls:
        name, args = call.name, call.arguments
        tool = decoded(name, args, call.id)
        digest = digest_line(name, args, tool)
        with open(LOG, 'a') as log:
            log.write(f"{time.strftime('%H:%M:%S')} {thread} {digest[:260]}\n")
        turns[thread].append(digest)
        enforce_storage(registry, thread, tool, record)
        if len(turns[thread]) >= 25:
            emit(turns, thread)
    if message.stop_reason.successful:
        text = ''.join(part.text for part in message.parts if isinstance(part, TextContent))
        emit(turns, thread, text)


def main(a):
    registry = Registration(Path(B) / 'registry.json')
    names, offsets = thread_names(registry), {}
    turns = collections.defaultdict(list)
    last_names = time.time()
    while True:
        if time.time() - last_names > 60:
            names.update(thread_names(registry)); last_names = time.time()
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
                names.update(thread_names(registry)); last_names = time.time()
            thread = names.get(path, path.split('/')[-2][:8])
            follow(registry, turns, thread, path, start, data[:cut])
        time.sleep(2)
