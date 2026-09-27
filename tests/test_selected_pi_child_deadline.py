"""Provider-free dedicated PID-namespace fake RPC lifecycle controls (Linux only)."""

from __future__ import annotations

import asyncio
import os
import select
import sys
import time
from pathlib import Path

import pytest

from agent_comms.selected_pi_child_deadline import (
    SelectedChildUnknown,
    _pidfd_open,
    arm_selected_child,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace candidate")

FAKE = r"""
import os, sys, time
p = os.fork()
if p == 0:
    os.setsid()  # escape the leader's initial process group, not its PID namespace
    while True: time.sleep(1)
open(sys.argv[1], 'w').write(str(p) + '\n')  # inner PID is not host PID: use NSpid mapping
line = sys.stdin.readline()
if line and sys.argv[2] == 'responsive': print('RPC:'+line.strip(), flush=True)
while True: time.sleep(1)
"""


def _outer_pid(inner_pid: int, marker: Path) -> int:
    # Resolve the nested namespace PID through /proc, never signal by host PID.
    for path in Path("/proc").glob("[0-9]*/status"):
        try:
            lines = path.read_text().splitlines()
            nspid = next(line for line in lines if line.startswith("NSpid:")).split()[1:]
            if (
                len(nspid) >= 2
                and int(nspid[-1]) == inner_pid
                and str(marker).encode() in path.with_name("cmdline").read_bytes()
            ):
                return int(nspid[0])
        except (OSError, StopIteration):
            continue
    raise AssertionError("Fake escaped child not found")


def _gone(pidfd: int) -> bool:
    return bool(select.select([pidfd], [], [], 0)[0])


async def _arm(tmp_path: Path, seconds: float, mode: str):
    pid_file = tmp_path / "escaped-pid"
    guard = await arm_selected_child(
        owner="fake-owner-epoch",
        session="fake-session",
        operation="selected-summary-op",
        command=(sys.executable, "-u", "-c", FAKE, str(pid_file), mode),
        deadline_ns=time.monotonic_ns() + int(seconds * 1e9),
        receipt=tmp_path / "receipt",
    )
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.02)
    assert pid_file.exists() and pid_file.read_text().strip(), "namespace fake must launch"
    host_pid = _outer_pid(int(pid_file.read_text()), pid_file)
    pidfd = _pidfd_open(host_pid)
    return guard, pidfd


@pytest.mark.asyncio
async def test_exact_rpc_and_sets_id_descendant_retires_on_owner_eof(tmp_path):
    guard, pidfd = await _arm(tmp_path, 5, "responsive")
    try:
        assert await guard.send(b"ping\n") == b"RPC:ping\n"
        row = await guard.retire()
        assert row["status"] == "unknown" and row["token"] == guard.identity.token
        assert _gone(pidfd), "PID namespace descendant survived guardian join"
        assert guard.guardian.returncode is not None
    finally:
        os.close(pidfd)


@pytest.mark.asyncio
async def test_unresponsive_selected_stream_hits_independent_deadline(tmp_path):
    guard, pidfd = await _arm(tmp_path, 1, "hang")
    try:
        with pytest.raises(SelectedChildUnknown):
            await guard.send(b"one-and-only-prompt\n")
        row = await guard.retire()
        assert row["status"] == "unknown"
        assert _gone(pidfd)
    finally:
        os.close(pidfd)


@pytest.mark.asyncio
async def test_cancelled_caller_still_retired_by_absolute_deadline(tmp_path):
    guard, pidfd = await _arm(tmp_path, 1, "hang")
    try:
        task = asyncio.create_task(guard.send(b"one-and-only-prompt\n"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Simulate caller abandoning the handle. Watchdog must retire even
        # before a cooperative owner closes stdin.
        await asyncio.sleep(1.2)
        assert _gone(pidfd)
        assert (await guard.retire())["status"] == "unknown"
    finally:
        os.close(pidfd)


@pytest.mark.asyncio
async def test_guardian_crash_kills_namespace_via_parent_death(tmp_path):
    guard, pidfd = await _arm(tmp_path, 5, "responsive")
    try:
        guard.guardian.kill()  # exact disposable fake guardian, never a worker
        await guard.guardian.wait()
        for _ in range(100):
            if _gone(pidfd):
                break
            await asyncio.sleep(0.02)
        assert _gone(pidfd), "namespace init/escaped descendant survived guardian loss"
        assert (await guard.retire())["status"] == "unknown"
    finally:
        os.close(pidfd)


@pytest.mark.asyncio
async def test_stale_incarnation_cannot_retire_next_operation(tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "next").mkdir()
    old, old_pidfd = await _arm(tmp_path / "old", 2, "responsive")
    try:
        assert await old.send(b"first\n") == b"RPC:first\n"
        assert (await old.retire())["status"] == "unknown"
        newer, next_pidfd = await _arm(tmp_path / "next", 4, "responsive")
        try:
            assert old.identity.token != newer.identity.token
            assert not _gone(next_pidfd)
            assert await newer.send(b"second\n") == b"RPC:second\n"
            assert (await newer.retire())["status"] == "unknown"
            assert _gone(next_pidfd)
        finally:
            os.close(next_pidfd)
    finally:
        os.close(old_pidfd)


@pytest.mark.asyncio
async def test_armed_handle_consumed_atomically_before_second_rpc(tmp_path):
    fake = (
        "import sys,time; "
        "[(print('RPC:'+sys.stdin.readline().strip(),flush=True)) for _ in range(2)]; "
        "time.sleep(30)"
    )
    guard = await arm_selected_child(
        owner="owner",
        session="session",
        operation="once",
        command=(sys.executable, "-u", "-c", fake),
        deadline_ns=time.monotonic_ns() + 4_000_000_000,
        receipt=tmp_path / "once",
    )
    assert await guard.send(b"first\n") == b"RPC:first\n"
    with pytest.raises(SelectedChildUnknown, match="already dispatched"):
        await guard.send(b"second\n")
    assert (await guard.retire())["status"] == "unknown"


@pytest.mark.asyncio
async def test_unshare_denial_never_reports_namespace_ready(tmp_path, monkeypatch):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    shim = fakebin / "unshare"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
    marker = tmp_path / "never-executed"
    with pytest.raises(SelectedChildUnknown, match="arm UNKNOWN"):
        await arm_selected_child(
            owner="owner",
            session="session",
            operation="denied",
            command=(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('BAD')"),
            deadline_ns=time.monotonic_ns() + 3_000_000_000,
            receipt=tmp_path / "denied",
        )
    assert not marker.exists()
    assert (tmp_path / "denied").exists(), "UNKNOWN is durable before unshare attempt"


@pytest.mark.asyncio
async def test_delayed_popen_cannot_exec_fake_after_absolute_deadline(tmp_path, monkeypatch):
    hookdir = tmp_path / "hook"
    hookdir.mkdir()
    (hookdir / "sitecustomize.py").write_text(
        "import subprocess,time\n"
        "original=subprocess.Popen\n"
        "def delayed(*args,**kwargs):\n"
        "    proc=original(*args,**kwargs)\n"
        "    time.sleep(2.2)\n"
        "    return proc\n"
        "subprocess.Popen=delayed\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(hookdir) + os.pathsep + os.environ["PYTHONPATH"])
    marker = tmp_path / "late-exec"
    with pytest.raises(SelectedChildUnknown, match="arm UNKNOWN"):
        await arm_selected_child(
            owner="owner",
            session="session",
            operation="slow-spawn",
            command=(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('BAD')"),
            deadline_ns=time.monotonic_ns() + 900_000_000,
            receipt=tmp_path / "slow",
        )
    # The hook is active only in the disposable guardian/namespace fake; it
    # returns after the deadline. No native command may cross the wrapper gate.
    await asyncio.sleep(1.5)
    assert not marker.exists()
    assert (tmp_path / "slow").exists()


@pytest.mark.asyncio
async def test_stalled_popen_does_not_leave_live_guardian_or_namespace(tmp_path, monkeypatch):
    hookdir = tmp_path / "hook"
    hookdir.mkdir()
    pid_file = tmp_path / "pids"
    (hookdir / "sitecustomize.py").write_text(
        "import os,sys,subprocess,time\n"
        "if '--guardian' in sys.argv and os.environ.get('FAULT_PID_FILE'):\n"
        " original=subprocess.Popen\n"
        " def delayed(*a,**kw):\n"
        "  p=original(*a,**kw)\n"
        "  with open(os.environ['FAULT_PID_FILE'],'w') as f: "
        "f.write(str(os.getpid())+' '+str(p.pid))\n"
        "  time.sleep(4.5)\n"
        "  return p\n"
        " subprocess.Popen=delayed\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(hookdir) + os.pathsep + os.environ["PYTHONPATH"])
    monkeypatch.setenv("FAULT_PID_FILE", str(pid_file))
    marker = tmp_path / "never-started"
    arm = asyncio.create_task(
        arm_selected_child(
            owner="owner",
            session="session",
            operation="stalled-spawn",
            command=(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('BAD')"),
            deadline_ns=time.monotonic_ns() + 1_000_000_000,
            receipt=tmp_path / "stalled",
        )
    )
    wrapper_fd = None
    for _ in range(80):
        if pid_file.exists() and pid_file.read_text().strip():
            guardian_pid, unshare_pid = map(int, pid_file.read_text().split())
            children = Path(f"/proc/{unshare_pid}/task/{unshare_pid}/children")
            if children.exists() and children.read_text().strip():
                wrapper_pid = int(children.read_text().split()[0])
                wrapper_fd = _pidfd_open(wrapper_pid)
                break
        await asyncio.sleep(0.01)
    assert wrapper_fd is not None, "Must hold exact staged PID namespace init pidfd"
    try:
        with pytest.raises(SelectedChildUnknown, match="arm UNKNOWN"):
            await arm
        await asyncio.sleep(1)
        assert _gone(wrapper_fd), "Staged PID namespace init still alive past deadline"
    finally:
        os.close(wrapper_fd)

    def live(pid: int) -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
            return state != "Z"
        except FileNotFoundError:
            return False

    assert not live(guardian_pid)
    assert not live(unshare_pid)
    assert not marker.exists()
    assert (tmp_path / "stalled").exists()


@pytest.mark.asyncio
async def test_missing_namespace_launcher_fails_before_native_spawn(tmp_path, monkeypatch):
    from agent_comms import selected_pi_child_deadline as mod

    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    with pytest.raises(SelectedChildUnknown, match="unavailable"):
        await mod.arm_selected_child(
            owner="o",
            session="s",
            operation="op",
            command=(sys.executable, "-c", 'raise AssertionError("should not run")'),
            deadline_ns=time.monotonic_ns() + 1_000_000_000,
            receipt=tmp_path / "receipt",
        )
    assert not (tmp_path / "receipt").exists()
