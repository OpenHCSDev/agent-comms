"""Physical locking, not fake store objects: nesting must keep outsiders out."""
import asyncio
from contextvars import copy_context
import fcntl
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.store_files import (
    SharedLock, StoreLockContention, _async_store_lock, _store_lock, store_lock_path,
)


def probe(path, *, shared=True):
    """A fresh interpreter cannot inherit our process-local acquisition context."""
    result = subprocess.run([
        sys.executable, '-c',
        'import fcntl,sys\n'
        'with open(sys.argv[1], "rb") as stream:\n'
        ' try: fcntl.flock(stream, int(sys.argv[2]) | fcntl.LOCK_NB)\n'
        ' except BlockingIOError: sys.exit(73)\n',
        str(store_lock_path(path)), str(fcntl.LOCK_SH if shared else fcntl.LOCK_EX),
    ], capture_output=True, text=True, timeout=5)
    assert result.returncode in (0, 73), result.stderr
    return result.returncode == 0


def test_nested_reader_reuses_descriptor_without_downgrading_or_releasing(tmp_path):
    path = tmp_path / 'wire'
    with _store_lock(path) as outer:
        with pytest.raises(ValueError, match='consumer failed'):
            with _store_lock(path, mode=SharedLock(), contention=StoreLockContention(0)) as inner:
                assert inner.descriptor == outer.descriptor
                links = []
                for descriptor in Path('/proc/self/fd').iterdir():
                    try:
                        links.append(descriptor.readlink())
                    except FileNotFoundError:
                        pass
                assert links.count(store_lock_path(path)) == 1
                assert not probe(path)
                raise ValueError('consumer failed')
        assert not probe(path)
    assert probe(path, shared=False)


def test_shared_nesting_never_silently_upgrades(tmp_path):
    path = tmp_path / 'wire'
    with _store_lock(path, mode=SharedLock()) as outer:
        with _store_lock(path, mode=SharedLock()) as inner:
            assert inner.descriptor == outer.descriptor
            assert probe(path)
            assert not probe(path, shared=False)
        with pytest.raises(RelationViolationError, match='Cannot upgrade'):
            with _store_lock(path):
                pytest.fail('upgraded the enclosing read transaction')
    assert probe(path, shared=False)


def test_ended_context_does_not_reuse_its_closed_descriptor(tmp_path):
    path = tmp_path / 'wire'
    with _store_lock(path):
        retained = copy_context()
    def capture_again():
        with _store_lock(path):
            assert not probe(path)
    retained.run(capture_again)
    assert probe(path, shared=False)


def test_nested_source_still_checks_each_borrows_budget(tmp_path):
    path = tmp_path / 'wire'
    path.write_bytes(b'not within the nested budget')
    with _store_lock(path):
        with pytest.raises(RelationViolationError, match='bounded read budget'):
            with _store_lock(path, mode=SharedLock(), max_bus_bytes=1):
                pytest.fail('nested source skipped its read budget')
        assert not probe(path)
    assert probe(path, shared=False)


def test_async_and_sync_nest_but_independent_tasks_and_threads_do_not(tmp_path):
    path = tmp_path / 'wire'
    async def independent():
        with pytest.raises(BlockingIOError):
            async with _async_store_lock(path, mode=SharedLock(), blocking=False):
                pytest.fail('another task borrowed an exclusive transaction')
    def thread_reader():
        with pytest.raises(BlockingIOError):
            with _store_lock(path, mode=SharedLock(), blocking=False):
                pytest.fail('another thread borrowed an exclusive transaction')
    async def run():
        async with _async_store_lock(path) as outer:
            async with _async_store_lock(path, mode=SharedLock()) as inner:
                assert inner.descriptor == outer.descriptor
                with _store_lock(path, mode=SharedLock()) as synchronous:
                    assert synchronous.descriptor == outer.descriptor
                assert not probe(path)
            await asyncio.create_task(independent())
            await asyncio.to_thread(thread_reader)
            assert not probe(path)
        assert probe(path, shared=False)
    asyncio.run(run())


def test_checked_async_worker_explicitly_borrows_the_original_acquisition(tmp_path):
    comms = wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    async def run():
        with comms.bus.log.locked() as outer:
            def read(source):
                # The joined worker may itself nest a normal source acquisition.
                with comms.bus.log.locked(mode=SharedLock()) as inner:
                    assert inner.descriptor == outer.descriptor
                return source.witness.through_seq
            assert await comms.bus.log.read_checked_async(read) == 0
            assert not probe(comms.bus.log.path)
        assert probe(comms.bus.log.path, shared=False)
    asyncio.run(run())
