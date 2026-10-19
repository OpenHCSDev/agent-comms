"""Follow live native sessions and print turn digests for commander judgment."""
import os, time, glob, collections

from . import order
from ..native_tool_call import CodingCall, SelectedToolDenied
from ..native_entries import NativeEntry
from ..pi_payloads import PiMessage, TextContent
from ..tool_declaration import one_line
from ..registration import Registration

def thread_names(registry: Registration) -> dict[str, str]:
    """Session file -> thread name, from the registry snapshot's threads."""
    return {thread.session_file: name for name, thread in registry.snapshot().threads.items()
            if thread.session_file}


def enforce_storage(registry: Registration, thread: str, call: CodingCall) -> None:
    """Explain a refused storage operation without punishing its unit."""
    if thread not in order.units() or thread not in registry:
        return
    try:
        call.decide_storage(registry, thread)
    except SelectedToolDenied as violation:
        print(f'VIOLATION storage {thread}: {violation}', flush=True)


def emit(turns, thread, final=''):
    calls = turns.pop(thread, [])
    if not calls and (not final or final.strip() == '{"decision":"IGNORE"}'):
        return
    lines = [f'TURN {thread} ({len(calls)} calls)']
    lines += [f'  {c}' for c in calls[-14:]]
    if final:
        lines.append(f'  says: {one_line(final, 400)}')
    print('\n'.join(lines)[:3000], flush=True)


def follow(registry: Registration, turns: dict[str, list[str]], thread: str, data: bytes) -> None:
    for line in data.splitlines(keepends=True):
        entry = NativeEntry.read(line)
        for message in entry.messages:
            if message.assistant:
                walk(registry, turns, thread, message)


def walk(registry: Registration, turns: dict[str, list[str]], thread: str,
         message: PiMessage) -> None:
    for pi_call in message.tool_calls:
        coding_call = CodingCall.decode(pi_call.id, pi_call.name, pi_call.arguments)
        digest = coding_call.digest()
        turns[thread].append(digest)
        enforce_storage(registry, thread, coding_call)
        if len(turns[thread]) >= 25:
            emit(turns, thread)
    if message.stop_reason.successful:
        text = ''.join(part.text for part in message.parts if isinstance(part, TextContent))
        emit(turns, thread, text)


def main(a):
    registry = order.COMMS.registry
    names, offsets = thread_names(registry), {}
    turns = collections.defaultdict(list)
    last_names = time.time()
    while True:
        if time.time() - last_names > 60:
            names.update(thread_names(registry)); last_names = time.time()
        for path in set(glob.glob(f'{order.COMMS.root}/native-sessions/*/*.jsonl')) | set(names):
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
            follow(registry, turns, thread, data[:cut])
        time.sleep(2)
