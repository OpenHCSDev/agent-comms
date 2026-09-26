"""Provider-free registry authority-span and inherited-flock crash contracts."""

from __future__ import annotations

import errno
import os
import selectors
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from agent_comms.declarations import Goal, Thread, ThreadRegistry, _store_lock
from agent_comms.owner_compaction_process import (
    CompactionTransportUnknownError,
    run_authority_child,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX inherited flock contract")


def owner_guard(root: Path):
    registry = ThreadRegistry(root / "registry.json")
    owner = Thread("owner", frozenset(), str(root), pid=os.getpid(), goal=Goal("task", "g"))
    registry.register(owner)
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    return registry.guard_owner_compaction(
        owner,
        epoch,
        "turn",
        expected_goal_id="g",
        expected_goal_revision=owner.goal.revision,
        correction_revision=0,
        session_file=str(root / "native.jsonl"),
        session_leaf="leaf",
        session_revision="revision",
    )


def line(stream) -> bytes:
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        assert selector.select(5), "child did not reach barrier"
        return stream.readline()


def assert_locked(root: Path, locked: bool) -> None:
    import fcntl

    with (root / ".registry.json.lock").open("ab") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            assert error.errno in (errno.EAGAIN, errno.EACCES)
            assert locked
        else:
            assert not locked


@pytest.mark.parametrize("mutation", ["stop", "heartbeat", "goal"])
def test_registry_writers_wait_through_mutation(tmp_path, mutation):
    script = """
import fcntl, sys
from dataclasses import replace
from pathlib import Path
from agent_comms.declarations import ThreadRegistry, Goal
root = Path(sys.argv[1])
with (root / '.registry.json.lock').open('ab') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('blocked', flush=True)
    else:
        raise AssertionError('registry authority escaped')
registry = ThreadRegistry(root / 'registry.json')
mutation = sys.argv[2]
if mutation == 'stop':
    registry.unregister('owner')
elif mutation == 'heartbeat':
    registry.heartbeat('owner')
else:
    owner = registry.snapshot().threads['owner']
    registry.register(replace(owner, goal=Goal('new task', 'new-goal')))
print('finished', flush=True)
"""
    child = None
    try:
        with owner_guard(tmp_path) as (receipt, fd):
            assert receipt.thread == "owner"
            os.fstat(fd)
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path), mutation],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            assert line(child.stdout) == b"blocked\n"
            # Simulates the native mutation boundary while the real competing
            # registry writer is excluded, not a precheck/release/postcheck.
            assert_locked(tmp_path, True)
            (tmp_path / "mutation").write_text("committed")
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert stdout == b"finished\n"
        assert_locked(tmp_path, False)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


def test_child_retains_authority_after_scope_exception(tmp_path):
    # Even a caller accidentally unwinding before reaping cannot explicitly
    # unlock authority underneath an inherited child. Closing last FD releases.
    read_fd, write_fd = os.pipe()
    child = None
    try:
        with pytest.raises(RuntimeError, match="cancel"), owner_guard(tmp_path) as (_, fd):
            child = subprocess.Popen(
                [sys.executable, "-c", "import os,sys; os.read(int(sys.argv[1]),1)", str(read_fd)],
                pass_fds=(fd, read_fd),
            )
            raise RuntimeError("cancel")
        assert_locked(tmp_path, True)
        os.write(write_fd, b"x")
        assert child.wait(timeout=5) == 0
        assert_locked(tmp_path, False)
    finally:
        os.close(read_fd)
        os.close(write_fd)
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


def test_bounded_child_inherits_authority_and_releases(tmp_path):
    with owner_guard(tmp_path) as (_, fd):
        result = run_authority_child(
            [
                sys.executable,
                "-c",
                "import os,sys; os.fstat(int(sys.argv[1])); print('done')",
                str(fd),
            ],
            b"request",
            authority_fd=fd,
            timeout=5,
        )
        assert result.returncode == 0
        assert result.stdout == b"done\n"
        assert_locked(tmp_path, True)
    assert_locked(tmp_path, False)


def test_timeout_kills_child_before_authority_release(tmp_path):
    with owner_guard(tmp_path) as (_, fd):
        with pytest.raises(CompactionTransportUnknownError, match="never replay"):
            run_authority_child(
                [sys.executable, "-c", "import signal; signal.pause()"],
                b"request",
                authority_fd=fd,
                timeout=0.1,
            )
        assert_locked(tmp_path, True)
    assert_locked(tmp_path, False)


def test_cancellation_reaps_child_before_unwind(tmp_path, monkeypatch):
    children = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(real_popen, "communicate", cancelled)
    with pytest.raises(KeyboardInterrupt), owner_guard(tmp_path) as (_, fd):
        run_authority_child(
            [sys.executable, "-c", "import signal; signal.pause()"],
            b"request",
            authority_fd=fd,
            timeout=5,
        )
    assert len(children) == 1
    assert children[0].returncode == -signal.SIGKILL
    assert_locked(tmp_path, False)


def test_parent_sigkill_cannot_release_child_authority(tmp_path):
    read_fd, write_fd = os.pipe()
    script = """
import os, signal, subprocess, sys
from pathlib import Path
from agent_comms.declarations import _store_lock
root = Path(sys.argv[1])
with _store_lock(root / 'registry.json') as fd:
    child = subprocess.Popen(
        [sys.executable, '-c',
         'import os,sys; print("ready",flush=True); os.read(int(sys.argv[1]),1); '
         'open(sys.argv[2],"w").write("mutation"); os.close(int(sys.argv[3])); '
         'print("done",flush=True)',
         sys.argv[2], str(root / 'mutation'), str(fd)],
        pass_fds=(fd, int(sys.argv[2])),
    )
    print(child.pid, flush=True)
    signal.pause()
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(read_fd)],
        pass_fds=(read_fd,),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    child_pid = None
    try:
        lines = [line(parent.stdout).strip(), line(parent.stdout).strip()]
        assert b"ready" in lines
        child_pid = int(next(value for value in lines if value != b"ready"))
        parent.kill()
        assert parent.wait(timeout=5) == -signal.SIGKILL
        assert_locked(tmp_path, True)
        assert not (tmp_path / "mutation").exists()
        os.write(write_fd, b"x")
        assert line(parent.stdout) == b"done\n"
        child_pid = None  # Child has dropped authority and is exiting; do not signal a reused PID.
        assert (tmp_path / "mutation").read_text() == "mutation"
        assert_locked(tmp_path, False)
    finally:
        os.close(read_fd)
        os.close(write_fd)
        if parent.poll() is None:
            parent.kill()
        parent.wait()
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
        parent.stdout.close()
        parent.stderr.close()


@pytest.mark.parametrize("deadline", [0, -1, 31, float("nan"), float("inf")])
def test_bad_deadline_refuses_before_spawn(tmp_path, deadline):
    with _store_lock(tmp_path / "registry.json") as fd, pytest.raises(ValueError):
        run_authority_child(["must-not-execute"], b"", authority_fd=fd, timeout=deadline)
