"""Workers forked from this process through its template: they are other processes holding
this one's loaded modules, a failure crosses as itself, a lost worker is replaced, and
shutdown leaves none."""

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from agent_comms.execution.child_process import ForkedStart, SpawnedHelper
from agent_comms.execution.helper_workers import ForkedTemplate, LostHelperWorkerError

LOADED_BEFORE_FORK = object()
"""Module state only this interpreter holds: a fresh interpreter importing this file would
make another object, so a worker that reports this identity was forked, never started."""

_initialized: list[str] = []


def initialize(word: str) -> None:
    _initialized.append(word)


def where() -> tuple[int, int, int, tuple[str, ...]]:
    return (os.getpid(), os.getppid(), id(sys.modules[__name__].LOADED_BEFORE_FORK),
            tuple(_initialized))


def threads() -> int:
    return len(os.listdir("/proc/self/task"))


def fail() -> None:
    raise ValueError("no such row")


def refused() -> None:
    """What a worker's HTTP call raises when the bus answers 503: an exception pickle can
    write and cannot rebuild."""
    raise urllib.error.HTTPError("https://bus.example/mcp", 503, "Service Unavailable", {}, None)


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def held(release: str) -> int:
    while not Path(release).exists():
        time.sleep(0.01)
    return os.getpid()


@pytest.fixture
def workers():
    made = ForkedStart().workers(2, initialize, ("ready",))
    yield made
    made.shutdown(wait=True)


def test_workers_are_forks_of_the_one_thread_template_holding_this_process_modules(workers):
    # Threads this process holds when the template is forked do not reach the workers.
    stop = threading.Event()
    extra = [threading.Thread(target=stop.wait) for _ in range(3)]
    for thread in extra:
        thread.start()
    try:
        workers.start()
        answers = [workers.submit(where).result(timeout=10) for _ in range(6)]
    finally:
        stop.set()
        for thread in extra:
            thread.join()
    template = ForkedTemplate.current()
    pids = {pid for pid, _parent, _marker, _words in answers}
    assert 1 <= len(pids) <= 2 and os.getpid() not in pids and template.pid not in pids
    assert {parent for _pid, parent, _marker, _words in answers} == {template.pid}
    # The same object identity: no worker imported this module; each was born holding it.
    assert {marker for _pid, _parent, marker, _words in answers} == {id(LOADED_BEFORE_FORK)}
    assert {words for _pid, _parent, _marker, words in answers} == {("ready",)}
    assert workers.submit(threads).result(timeout=10) == 1
    assert len(os.listdir(f"/proc/{template.pid}/task")) == 1


def test_a_failure_crosses_as_itself_and_the_worker_serves_the_next_call(workers):
    with pytest.raises(ValueError, match="no such row") as raised:
        workers.submit(fail).result(timeout=10)
    assert "In helper worker" in "".join(raised.value.__notes__)
    assert workers.submit(where).result(timeout=10)[0] != os.getpid()


@pytest.mark.parametrize("start", [ForkedStart, SpawnedHelper])
def test_a_failure_pickle_cannot_rebuild_crosses_as_its_builtin_ancestor(start):
    """Each member's caller gets an OSError naming the HTTPError, with where it was raised,
    and the workers serve the next call: no TypeError about a constructor, no broken pool."""
    made = start().workers(1)
    try:
        with pytest.raises(OSError, match="HTTPError: HTTP Error 503: Service Unavailable") as raised:
            made.submit(refused).result(timeout=60)
        assert type(raised.value) is OSError
        assert "In helper worker" in "".join(raised.value.__notes__)
        assert made.submit(threads).result(timeout=60) >= 1
    finally:
        made.shutdown(wait=True)


def test_a_worker_lost_under_a_call_fails_it_and_is_replaced(workers):
    with pytest.raises(LostHelperWorkerError):
        workers.submit(die).result(timeout=10)
    assert workers.submit(where).result(timeout=10)[0] != os.getpid()


def test_a_worker_killed_while_idle_is_replaced_before_the_next_call(workers):
    workers.start()
    first = {workers.submit(where).result(timeout=10)[0] for _ in range(4)}
    for pid in first:
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while any(Path(f"/proc/{pid}").exists() for pid in first):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    # As Android kills an app's children while it is in the background: no call is lost.
    after = {workers.submit(where).result(timeout=10)[0] for _ in range(4)}
    assert after and not after & first


def test_a_lost_template_is_forked_again(workers):
    workers.start()
    lost = ForkedTemplate.current()
    os.kill(lost.pid, signal.SIGKILL)
    os.waitpid(lost.pid, 0)
    for pid in {workers.submit(where).result(timeout=10)[0] for _ in range(4)}:
        os.kill(pid, signal.SIGKILL)
    time.sleep(0.05)
    pid, parent, _marker, _words = workers.submit(where).result(timeout=10)
    assert parent == ForkedTemplate.current().pid != lost.pid


def test_calls_beyond_the_workers_wait_and_shutdown_leaves_no_worker(tmp_path):
    workers = ForkedStart().workers(2)
    release = tmp_path / "release"
    calls = [workers.submit(held, str(release)) for _ in range(5)]
    time.sleep(0.2)
    assert not any(call.done() for call in calls)
    release.touch()
    pids = {call.result(timeout=10) for call in calls}
    assert len(pids) <= 2
    workers.shutdown(wait=True)
    assert not any(Path(f"/proc/{pid}").exists() for pid in pids)
    with pytest.raises(RuntimeError, match="shutdown"):
        workers.submit(where)


def test_terminating_stops_a_running_call(tmp_path):
    workers = ForkedStart().workers(1)
    call = workers.submit(held, str(tmp_path / "never"))
    time.sleep(0.2)
    workers.terminate_workers()
    with pytest.raises(LostHelperWorkerError):
        call.result(timeout=10)
    workers.shutdown(wait=True)


def test_child_process_and_connector_profile_import_without_multiprocessing_extension():
    """Android's Python build has no _multiprocessing C extension (bionic lacks POSIX
    named semaphores), so importing child_process or connector_profile must never load it."""
    probe = (
        "import sys; sys.modules['_multiprocessing'] = None; "
        "import agent_comms.execution.child_process; "
        "import agent_comms.transport.connector_profile"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
