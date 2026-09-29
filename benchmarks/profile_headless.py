"""S7 real send/list contention: three poller processes, no model or mock APIs.

Run with an installed agent-comms wheel, e.g.:
  python benchmarks/profile_headless.py --scratch ~/wt/s7-disposable --output receipt.jsonl

This replaces the removed Comms facade and private transcript-replay benchmark.
The exclusive comparison changes only shared-lock requests to real LOCK_EX;
all durable barriers, writes, checkpoints and current store owners still run.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import platform
import resource
import shutil
import signal
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

import agent_comms
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread

from lock_observation import ReadPolicy, LockObservation, distribution


@dataclass(frozen=True)
class Budget:
    sends: int
    seed: int
    poll_interval: float
    send_interval: float
    seconds: int
    ram_mb: int
    disk_mb: int
    lock_events: int

    def install(self):
        maximum = self.ram_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))

    def check_disk(self, root):
        size = 0
        for path in root.rglob("*"):
            try:
                size += path.stat().st_size
            except FileNotFoundError:
                # Real concurrent snapshot replacement can unlink a staging file.
                continue
        if size > self.disk_mb * 1024 * 1024:
            raise RuntimeError("Benchmark scratch disk budget exceeded")
        if shutil.disk_usage(root).free < 1024 * 1024 * 1024:
            raise RuntimeError("Less than 1 GiB free on scratch filesystem")
        return size


def join_maintenance():
    # Keep the current production maintenance path. Await our isolated process's
    # actual daemon before uninstalling timing hooks or deleting its owned root.
    for thread in threading.enumerate():
        if thread.name == "agent-comms-candidate-maintenance":
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError("Owned candidate maintenance did not stop")


def poller(root, policy, budget, ready, start, stop, output):
    budget.install()
    comms = Comms(root)
    durations = []
    result = {}
    failures = {}
    try:
        assert len(comms.views.list_threads()) > 0
        ready.put(os.getpid())
        if not start.wait(budget.seconds):
            raise TimeoutError("Poller start timeout")
        with LockObservation(policy, budget.lock_events) as locks:
            while not stop.is_set():
                started = time.perf_counter()
                try:
                    comms.views.list_threads()
                except RelationViolationError as error:
                    # A real fail-closed read is an observed failed poll, not a
                    # successful sample or permission to bypass the guard.
                    message = f"{type(error).__name__}: {error}"
                    failures[message] = failures.get(message, 0) + 1
                durations.append((time.perf_counter() - started) * 1000)
                stop.wait(budget.poll_interval)
            result = {"poll_all_attempts": distribution(durations),
                      "failed_polls": failures, "locks": locks.report(),
                      "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    except BaseException:
        result["error"] = traceback.format_exc()
        raise
    finally:
        output.write_text(json.dumps(result))


def deadline(_signum, _frame):
    raise TimeoutError("Benchmark case deadline exceeded")


def run_case(scratch, count, policy, budget):
    context = mp.get_context("spawn")
    start, stop, ready = context.Event(), context.Event(), context.Queue()
    workers = []
    with tempfile.TemporaryDirectory(prefix=f"s7-{count}-", dir=scratch) as directory:
        root = Path(directory)
        wire_root = root / "wire"
        comms = Comms(wire_root)
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(budget.seconds)
        try:
            comms.messaging.initialize_private_initial_protocol()
            identity = ProcessIdentity.capture(os.getpid())
            for index in range(count):
                comms.registry.declare(Thread(f"thread-{index}", frozenset({"benchmark"}),
                                              str(root), process_identity=identity))
            for index in range(budget.seed):
                comms.messaging.send("thread-0", "#benchmark", f"seed {index}")
            join_maintenance()
            assert comms.bus.log.claim_gate_enabled(), "Must measure the durable private bus"
            assert len(comms.views.list_threads()) == count
            for index in range(3):
                worker = context.Process(target=poller, args=(
                    wire_root, policy, budget, ready, start, stop, root / f"poller-{index}.json"))
                worker.start()
                workers.append(worker)
            for _ in workers:
                ready.get(timeout=budget.seconds)
            sends = []
            started_case = time.perf_counter()
            with LockObservation(policy, budget.lock_events) as locks:
                start.set()
                for index in range(budget.sends):
                    started = time.perf_counter()
                    comms.messaging.send("thread-0", "#benchmark", f"measured {index}")
                    sends.append((time.perf_counter() - started) * 1000)
                    if index % 10 == 0:
                        budget.check_disk(root)
                        if any(not worker.is_alive() for worker in workers):
                            raise RuntimeError("A poller exited during measurement")
                    time.sleep(budget.send_interval)
                elapsed = time.perf_counter() - started_case
                stop.set()
                for worker in workers:
                    worker.join(timeout=15)
                    if worker.exitcode != 0:
                        raise RuntimeError(f"Poller {worker.pid} failed: {worker.exitcode}")
                join_maintenance()
                sender_locks = locks.report()
            reports = [json.loads((root / f"poller-{i}.json").read_text()) for i in range(3)]
            assert all(report["poll_all_attempts"]["count"] > 0 for report in reports)
            rows = comms.views.full_history()
            assert len(rows) == budget.seed + budget.sends, "All measured sends must persist"
            return {"threads": count, "policy": policy.name, "pollers": 3,
                    "elapsed_seconds": elapsed, "send": distribution(sends),
                    "sender_locks": sender_locks, "poller_results": reports,
                    "scratch_bytes": budget.check_disk(root), "durable_messages": len(rows),
                    "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        finally:
            signal.alarm(0)
            stop.set()
            for worker in workers:
                worker.join(timeout=2)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=2)
                if worker.is_alive():
                    worker.kill()
                    worker.join()
            join_maintenance()
            ready.close()
            ready.join_thread()


def persistent_scratch(path):
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields, filesystem = line.split(" - ", 1)
        mount = Path(fields.split()[4].replace("\\040", " "))
        if resolved.is_relative_to(mount):
            mounts.append((len(mount.parts), filesystem.split()[0]))
    if max(mounts)[1] in {"tmpfs", "ramfs"}:
        raise ValueError("Benchmark scratch must be on a persistent filesystem")
    return resolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, nargs="+", default=[50, 100, 150])
    parser.add_argument("--sends", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20)
    parser.add_argument("--poll-interval", type=float, default=.02)
    parser.add_argument("--send-interval", type=float, default=.01)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--ram-mb", type=int, default=768)
    parser.add_argument("--disk-mb", type=int, default=128)
    parser.add_argument("--lock-events", type=int, default=500000)
    args = parser.parse_args()
    budget = Budget(**{key: value for key, value in vars(args).items()
                       if key not in {"scratch", "output", "threads"}})
    if min(asdict(budget).values()) <= 0 or min(args.threads) < 2:
        parser.error("Budgets must be positive; at least two threads are required")
    scratch = persistent_scratch(args.scratch)
    budget.install()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as output:
        output.write(json.dumps({"environment": {"python": platform.python_version(),
            "platform": platform.platform(), "installed_package": agent_comms.__file__,
            "scratch": str(scratch), "budget": asdict(budget),
            "cpu_count": os.cpu_count(), "load_average": os.getloadavg()}}) + "\n")
        output.flush()
        for index, count in enumerate(args.threads):
            policies = [policy() for policy in ReadPolicy.__subclasses__()]
            if index % 2:
                policies.reverse()  # Counterbalance run order; not statistical randomization.
            for policy in policies:
                result = run_case(scratch, count, policy, budget)
                output.write(json.dumps(result) + "\n")
                output.flush()
                print(json.dumps({"threads": count, "policy": policy.name,
                                  "send": result["send"], "scratch_bytes": result["scratch_bytes"],
                                  "failed_polls": sum(sum(p["failed_polls"].values())
                                                      for p in result["poller_results"])}),
                      flush=True)


if __name__ == "__main__":
    main()
