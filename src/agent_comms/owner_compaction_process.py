"""Bounded Linux child lifetime inside a registry compaction authority scope.

Internal transport, not a compaction API or a receipt verifier. The command
must be a trusted, non-forking helper retaining inherited FDs until exit.
An independent pidfd watchdog survives owner death and enforces the absolute
deadline. Unsupported platforms fail closed; there is no parent-only fallback.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .compaction_child_watchdog import open_pidfd, signal_pidfd


class CompactionTransportUnknownError(RuntimeError):
    """The child may have mutated; reconciliation, not replay, is required."""


def require_deadline_support() -> None:
    """Refuse unsupported platforms/kernels before intent or native dispatch."""
    if sys.platform != "linux":
        raise NotImplementedError("Bounded inherited authority requires Linux pidfd watchdog")
    try:
        probe = open_pidfd(os.getpid())
        try:
            signal_pidfd(probe, 0)
        finally:
            os.close(probe)
    except OSError as error:
        raise NotImplementedError("Kernel pidfd deadline support unavailable") from error


def run_authority_child(
    command: Sequence[str],
    request: bytes,
    *,
    authority_fd: int,
    timeout: float,
    retained_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Dispatch once, only AFTER a separate exact-process watchdog is armed.

    The owner retains authority while waiting and reaping. If it dies during
    setup, the gated launcher sees EOF and never execs. Once armed, a sibling
    watchdog with NO authority FDs survives the owner and signals the exact
    native process at its deadline via pidfd (no PID-reuse race). Exec preserves
    native's owner-parent lineage. This is not an arbitrary-command public API.
    """
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Compaction child deadline must be in (0, 30] seconds")
    require_deadline_support()
    inherited = tuple(dict.fromkeys((authority_fd, *retained_fds)))
    for fd in inherited:
        os.fstat(fd)
    deadline = time.monotonic() + timeout
    gate_read, gate_write = os.pipe()
    child = watchdog = None
    pidfd = None
    scripts = Path(__file__).parent
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                str(scripts / "compaction_child_launcher.py"),
                str(gate_read),
                *command,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(*inherited, gate_read),
            start_new_session=True,
        )
        os.close(gate_read)
        gate_read = -1
        pidfd = open_pidfd(child.pid)
        watchdog = subprocess.Popen(
            [
                sys.executable,
                str(scripts / "compaction_child_watchdog.py"),
                str(pidfd),
                str(deadline),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(pidfd,),
            start_new_session=True,
        )
        assert watchdog.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(watchdog.stdout, selectors.EVENT_READ)
            if not selector.select(max(0, deadline - time.monotonic())):
                raise subprocess.TimeoutExpired(command, timeout)
        if watchdog.stdout.readline() != b"armed\n":
            raise OSError("Native deadline watchdog failed to arm")
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(command, timeout)
        os.write(gate_write, b"G")
        os.close(gate_write)
        gate_write = -1
        stdout, stderr = child.communicate(request, timeout=max(0, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise CompactionTransportUnknownError(
            "Native commit transport UNKNOWN; never replay"
        ) from error
    finally:
        # Includes BaseException cancellation. No explicit flock LOCK_UN:
        # inherited authority survives owner SIGKILL until native termination.
        for closing_fd in (gate_read, gate_write, pidfd):
            if closing_fd is not None and closing_fd >= 0:
                os.close(closing_fd)
        if child is not None:
            try:
                if child.returncode is None:
                    os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                child.wait()
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
        if watchdog is not None:
            if watchdog.poll() is None:
                watchdog.kill()
            watchdog.wait()
            if watchdog.stdout is not None:
                watchdog.stdout.close()
