"""Independent Linux pidfd deadline; survives owner SIGKILL without holding locks.

The pidfd identifies exactly the gated native process across exec, never a
reused PID. No authority descriptors or provider request data reach this child.
"""

from __future__ import annotations

import ctypes
import math
import os
import selectors
import signal
import sys
import time
from contextlib import suppress


def open_pidfd(pid: int) -> int:
    """Use libc when a portable Python build omitted Linux-only wrappers."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "pidfd_open", None)
    if function is None:
        raise NotImplementedError("Linux libc pidfd_open unavailable")
    function.argtypes = (ctypes.c_int, ctypes.c_uint)
    function.restype = ctypes.c_int
    descriptor = function(pid, 0)
    if descriptor < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(descriptor)


def signal_pidfd(pidfd: int, sig: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(pidfd, sig)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "pidfd_send_signal", None)
    if function is None:
        raise NotImplementedError("Linux libc pidfd_send_signal unavailable")
    function.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(pidfd, sig, None, 0) < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def main() -> None:
    pidfd, deadline = int(sys.argv[1]), float(sys.argv[2])
    if not math.isfinite(deadline):
        raise ValueError("Finite absolute native deadline required")
    os.fstat(pidfd)
    with selectors.DefaultSelector() as selector:
        selector.register(pidfd, selectors.EVENT_READ)
        print("armed", flush=True)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if selector.select(remaining):
                return  # Exact process exited, regardless of parent reaping.
    # Exact process may exit at the deadline; never fall back to signal by PID.
    with suppress(ProcessLookupError):
        signal_pidfd(pidfd, signal.SIGKILL)


if __name__ == "__main__":
    main()
