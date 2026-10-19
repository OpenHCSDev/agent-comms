from __future__ import annotations

import argparse
import faulthandler
import json
import multiprocessing as mp
import time
from dataclasses import replace
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path

from agent_comms.cli_bridges.claude import ClaudeTranscript
from agent_comms.comms import Comms
from agent_comms.storage_rules import StorageRoots
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.ui_model.observation import ObserveSidebar, SidebarObserved, SidebarUnreadable


def declaration(root: Path, name: str, created_at: float = 1) -> Thread:
    return Thread(
        name,
        frozenset({"proof"}),
        str(root),
        created_at=created_at,
        storage_roots=StorageRoots(root / "scratch" / name),
    )


def writer(root: Path, stop: Event, result: Queue[int], imports: bool) -> None:
    comms = Comms(root)
    count = 0
    while not stop.is_set():
        name = f"writer-{count % 85}"
        if imports and count < 85:
            comms.threads.import_thread(
                ClaudeTranscript(root / "source.jsonl"),
                name=name,
                worktree=str(root),
                tags=frozenset({"proof"}),
            )
        else:
            thread = (
                comms.registry.require(name)
                if imports
                else declaration(root, name, float(count % 85 + 1))
            )
            comms.registry.register(replace(thread, title=f"write {count}"), StoppedThreadStatus())
        count += 1
    result.put(count)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--imports", action="store_true")
    parser.add_argument("--dump-after", type=float, default=0)
    args = parser.parse_args()
    if args.dump_after:
        faulthandler.dump_traceback_later(args.dump_after)
    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    event = dict(
        timestamp="2026-10-19T00:00:00Z",
        cwd=str(root),
        sessionId="proof",
        promptId="p-1",
        origin=dict(kind="human"),
    )
    with (root / "source.jsonl").open("w") as stream:
        for kind in ("user", "assistant"):
            content = [dict(type="text", text=f"Scratch {kind}")]
            event.update(type=kind, uuid=kind, message=dict(role=kind, content=content))
            print(json.dumps(event), file=stream)
    comms.registry.register(declaration(root, "sentinel"), StoppedThreadStatus())
    reading = ObserveSidebar(str(root), True, False).observation()
    stop = mp.Event()
    counts: Queue[int] = mp.Queue()
    process = mp.Process(target=writer, args=(root, stop, counts, args.imports))
    process.start()
    start = time.monotonic()
    reads = empty = errors = 0
    try:
        while time.monotonic() - start < args.seconds:
            for result in reading.read(comms, comms.views.revision()):
                if isinstance(result, SidebarObserved):
                    reads += 1
                    if not result.derived[1]:
                        empty += 1
                        print(f"EMPTY after {reads} reads", flush=True)
                elif isinstance(result, SidebarUnreadable):
                    errors += 1
                    print(f"UNREADABLE after {reads} reads: {result.error}", flush=True)
            if not process.is_alive():
                raise RuntimeError(f"writer exited: {process.exitcode}")
    finally:
        stop.set()
        process.join(15)
        comms.views.transcript_reads.close()
    print(
        f"elapsed={time.monotonic()-start:.3f}s reads={reads} empty={empty} errors={errors} "
        f"writes={counts.get(timeout=2)} writer_exit={process.exitcode} imports={args.imports}",
        flush=True,
    )
    if (reads > 0, empty, errors, process.exitcode) != (True, 0, 0, 0):
        raise RuntimeError("Sidebar observation failed; see the original results above")


if __name__ == "__main__":
    main()
