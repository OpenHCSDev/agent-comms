"""No-provider headless listing and transcript replay profile."""

import asyncio
import cProfile
import io
import json
import pstats
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from agent_comms import Thread, TurnRouting, wire
from agent_comms.acp import CommsAgent


def measured(call, repeats=7):
    values = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        values.append((time.perf_counter() - started) * 1000)
    return {"median_ms": round(statistics.median(values), 2), "max_ms": round(max(values), 2)}


def listing(count, messages):
    with tempfile.TemporaryDirectory(prefix="ac-headless-list-", dir="/var/tmp") as directory:
        root = Path(directory)
        comms = wire(root / "wire")
        for index in range(count):
            comms.register(Thread(f"thread-{index}", frozenset(), str(root)))
        for index in range(messages):
            comms.send("thread-0", "#all", f"message {index}")
        fresh = wire(root / "wire")
        cold = measured(lambda: wire(root / "wire").list_threads(), 3)
        warm = measured(fresh.list_threads, 9)
        print(
            json.dumps(
                {
                    "kind": "list_threads",
                    "threads": count,
                    "bus_messages": messages,
                    "cold": cold,
                    "warm": warm,
                }
            ),
            flush=True,
        )


class Client:
    def __init__(self, snapshots):
        self.transcript_snapshots = snapshots
        self.transcript_diffs = False
        self.count = 0

    async def session_update(self, **_kwargs):
        self.count += 1


def transcript(rows, routes):
    with tempfile.TemporaryDirectory(prefix="ac-headless-transcript-", dir="/var/tmp") as directory:
        root = Path(directory)
        session = root / "session.jsonl"
        with session.open("w") as output:
            for index in range(rows):
                output.write(
                    json.dumps(
                        {
                            "type": "message",
                            "id": f"entry-{index}",
                            "message": {"role": "assistant", "content": "x" * 80},
                        }
                    )
                    + "\n"
                )
        comms = wire(root / "wire")
        comms.register(Thread("worker", frozenset(), str(root), session_file=str(session)))
        if routes:
            comms.transcript_routes.record(
                str(session), tuple(f"entry-{index}" for index in range(routes)), TurnRouting()
            )
        comms = wire(root / "wire")

        def page():
            return comms.thread_transcript_page("worker")

        def tail():
            return comms.thread_transcript("worker")

        agent = CommsAgent(comms, runtime_enabled=False, auto_wake=False)
        agent._sessions["session"] = "worker"

        def replay(snapshots):
            client = Client(snapshots)
            asyncio.run(agent._replay_transcript("session", "worker", client=client))
            return client.count

        tracemalloc.start()
        started = time.perf_counter()
        cold_page = page()
        cold_ms = (time.perf_counter() - started) * 1000
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        warm_page = measured(page)
        warm_tail = measured(tail)
        snapshot = measured(lambda: replay(True), 3)
        legacy = measured(lambda: replay(False), 3)
        route_write = (
            measured(
                lambda: comms.transcript_routes.record(
                    str(session), ("new-annotation",), TurnRouting()
                )
            )
            if routes
            else None
        )
        profiler = cProfile.Profile()
        profiler.enable()
        page()
        profiler.disable()
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumtime").print_stats(7)
        print(
            json.dumps(
                {
                    "kind": "transcript",
                    "rows": rows,
                    "routes": routes,
                    "file_bytes": session.stat().st_size,
                    "cold_page_ms": round(cold_ms, 2),
                    "cold_peak_mb": round(peak / 1048576, 2),
                    "page_events": len(cold_page.events),
                    "warm_page": warm_page,
                    "warm_tail": warm_tail,
                    "snapshot_replay": snapshot,
                    "legacy_replay": legacy,
                    "route_write": route_write,
                    "profile": stream.getvalue(),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    for count in (25, 100, 400):
        listing(count, 500)
    for rows, routes in ((1000, 0), (10000, 0), (100000, 0), (100000, 10000), (100000, 100000)):
        transcript(rows, routes)
