"""Real scratch-bus sidebar observation concurrent with registry publication."""
import argparse
import json
import faulthandler
import multiprocessing as mp
from dataclasses import replace
import time
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.storage_rules import StorageRoots
from agent_comms.threads import Thread
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.ui_model.observation import ObserveSidebar, SidebarObserved, SidebarUnreadable


def writer(root, stop, ready, result, imports, dump_after):
    if dump_after:
        faulthandler.dump_traceback_later(dump_after)
    comms = Comms(root)
    ready.set()
    count = 0
    while not stop.is_set():
        name = f'writer-{count % 85}'
        if imports and count < 85:
            from agent_comms.cli_bridges.claude import ClaudeTranscript
            comms.threads.import_thread(ClaudeTranscript(root / 'source.jsonl'), name=name,
                                       worktree=str(root), tags=frozenset({'proof'}))
        elif imports:
            comms.registry.register(replace(comms.registry.require(name), title=f'write {count}'),
                                    StoppedThreadStatus())
        else:
            comms.registry.register(Thread(name, frozenset({'proof'}), str(root),
                created_at=float(count % 85 + 1), title=f'write {count}',
                storage_roots=StorageRoots(root / 'scratch' / name)), StoppedThreadStatus())
        count += 1
    result.put(count)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--imports', action='store_true')
    parser.add_argument('--dump-after', type=float, default=0)
    args = parser.parse_args()
    if args.dump_after:
        faulthandler.dump_traceback_later(args.dump_after)
    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    source = root / 'source.jsonl'
    source.write_text('\n'.join(json.dumps({
        'type': kind, 'uuid': str(index), 'timestamp': '2026-10-19T00:00:00Z',
        'cwd': str(root), 'sessionId': 'proof', 'promptId': 'p-1', 'origin': {'kind': 'human'},
        'message': {'role': kind, 'content': text if kind == 'user' else [{'type': 'text', 'text': text}]},
    }) for index, (kind, text) in enumerate((('user', 'Scratch source input'),
                                          ('assistant', 'Scratch source reply')))) + '\n')
    comms.registry.register(Thread('sentinel', frozenset({'proof'}), str(root),
        storage_roots=StorageRoots(root / 'scratch' / 'sentinel')), StoppedThreadStatus())
    request = ObserveSidebar(str(root), True, False)
    reading = request.observation()
    stop, ready, counts = mp.Event(), mp.Event(), mp.Queue()
    process = mp.Process(target=writer, args=(root, stop, ready, counts, args.imports, args.dump_after))
    process.start()
    ready.wait(10)
    start = time.monotonic()
    reads = empty = errors = 0
    try:
        while time.monotonic() - start < args.seconds:
            for result in reading.read(comms, comms.views.revision()):
                if isinstance(result, SidebarObserved):
                    reads += 1
                    if not result.derived[1]:
                        empty += 1
                        print(f'EMPTY after {reads} reads: registry={len(comms.registry.snapshot().threads)}', flush=True)
                elif isinstance(result, SidebarUnreadable):
                    errors += 1
                    print(f'UNREADABLE after {reads} reads: {result.error}', flush=True)
            if not process.is_alive():
                raise RuntimeError(f'writer exited: {process.exitcode}')
    finally:
        stop.set()
        process.join(15)
        comms.views.transcript_reads.close()
    print(f'elapsed={time.monotonic()-start:.3f}s reads={reads} empty={empty} errors={errors} writes={counts.get(timeout=2)} writer_exit={process.exitcode} imports={args.imports}', flush=True)
    if not reads or empty or errors or process.exitcode != 0:
        raise RuntimeError('Sidebar observation failed; see the original results above')


if __name__ == '__main__':
    main()
