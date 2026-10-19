"""Follow live native sessions and print turn digests for commander judgment."""
import collections
import glob
import os
import time
from pathlib import Path

from ..native_entries import NativeEntry
from ..native_tool_call import CodingCall, SelectedToolDenied
from ..pi_payloads import PiMessage, TextContent
from ..registration import Registration
from ..todos import StorageStrikeTodo
from ..tool_declaration import one_line
from .order import Commander


def thread_names(registry: Registration) -> dict[str, str]:
    """Session file -> thread name, from the registry snapshot's threads."""
    return {thread.session_file: name for name, thread in registry.snapshot().threads.items()
            if thread.session_file}


def enforce_storage(commander: Commander, thread: str, call: CodingCall) -> None:
    """Core judges its units and records each storage violation once."""
    registry = commander.comms.registry
    if thread not in commander.units() or thread not in registry:
        return
    try:
        registry.decide_storage(
            thread, call.storage_effects(Path(registry.require(thread).worktree))
        )
    except SelectedToolDenied as violation:
        print(f'VIOLATION storage {thread}: {violation}', flush=True)
        for todo in commander.held_by(thread)[:1]:
            commander.strike(thread, todo.id,
                         f'{todo.id} broke the storage rule at a tool call: {violation}; '
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


def follow(commander: Commander, turns: dict[str, list[str]], thread: str, data: bytes) -> None:
    for line in data.splitlines(keepends=True):
        entry = NativeEntry.read(line)
        for message in entry.messages:
            if message.assistant:
                walk(commander, turns, thread, message)


def walk(commander: Commander, turns: dict[str, list[str]], thread: str,
         message: PiMessage) -> None:
    for pi_call in message.tool_calls:
        coding_call = CodingCall.decode(pi_call.id, pi_call.name, pi_call.arguments)
        digest = coding_call.digest()
        turns[thread].append(digest)
        enforce_storage(commander, thread, coding_call)
        if len(turns[thread]) >= 25:
            emit(turns, thread)
    if message.stop_reason.successful:
        text = ''.join(part.text for part in message.parts if isinstance(part, TextContent))
        emit(turns, thread, text)


def main(a, commander):
    registry = commander.comms.registry
    names, offsets = thread_names(registry), {}
    turns = collections.defaultdict(list)
    last_names = time.time()
    while True:
        if time.time() - last_names > 60:
            names.update(thread_names(registry)); last_names = time.time()
        for path in set(glob.glob(f"{commander.comms.root}/native-sessions/*/*.jsonl")) | set(
            names
        ):
            try:
                st = os.stat(path)
            except FileNotFoundError:
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
            follow(commander, turns, thread, data[:cut])
        time.sleep(2)
