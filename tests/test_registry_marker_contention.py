"""Actual atomic marker publication versus independent registry readers."""

import multiprocessing as mp
import os
import time

import pytest

from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread


def _read_registry(root, ready, stop, result):
    comms = Comms(root)
    failures, reads = [], 0
    ready.put(os.getpid())
    try:
        while not stop.is_set():
            try:
                comms.registry.snapshot()
            except RelationViolationError as error:
                failures.append(str(error))
            reads += 1
    finally:
        result.put((reads, failures))


def test_concurrent_canonical_marker_publication_keeps_registry_readable(tmp_path):
    comms = Comms(tmp_path / "wire")
    comms.messaging.initialize_private_initial_protocol()
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path)))
    context = mp.get_context("spawn")
    ready, result, stop = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_read_registry, args=(comms.root, ready, stop, result))
               for _ in range(3)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            ready.get(timeout=20)
        started = time.monotonic()
        for _ in range(1500):
            # Use the real canonical marker publisher and durability wrapper;
            # no fake guard, patched syscall, replaced filesystem or sleep hook.
            with comms.bus.log.locked():
                metadata = comms.bus.log.read_metadata_unlocked(required=True)
                comms.bus.log.write_metadata_unlocked(metadata)
            assert time.monotonic() - started < 60, "Concurrent repro exceeded its time budget"
        stop.set()
        observations = [result.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        reads = sum(count for count, _ in observations)
        failures = [failure for _, errors in observations for failure in errors]
        print(f"actual concurrent marker publications=1500 readers=3 reads={reads} "
              f"failure_count={len(failures)} failures={failures[:10]}")
        assert reads >= 1500
        assert not failures
    finally:
        stop.set()
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=2)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=2)
                if worker.is_alive():
                    worker.kill()
                    worker.join()
        for queue in (ready, result):
            queue.close()
            queue.join_thread()


def _public_mode(path):
    path.chmod(0o644)


def _extra_link(path):
    os.link(path, path.with_suffix(".extra-link"))


def _redirect(path):
    target = path.with_suffix(".redirect-target")
    path.rename(target)
    path.symlink_to(target)


@pytest.mark.parametrize("damage", [_public_mode, _extra_link, _redirect], ids=lambda f: f.__name__)
def test_marker_ownership_failures_still_block_real_read_and_send(tmp_path, damage):
    comms = Comms(tmp_path / "wire")
    comms.messaging.initialize_private_initial_protocol()
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path)))
    comms.threads.register(Thread("peer", frozenset(), str(tmp_path)))
    bus = comms.bus.log.path.read_bytes()
    damage(comms.bus.log.metadata_path)
    with pytest.raises(RelationViolationError):
        comms.registry.snapshot()
    with pytest.raises(RelationViolationError):
        comms.bus.log.read_metadata_unlocked(required=True)
    with pytest.raises(RelationViolationError):
        comms.messaging.send("owner", "peer", "must not be published")
    assert comms.bus.log.path.read_bytes() == bus
