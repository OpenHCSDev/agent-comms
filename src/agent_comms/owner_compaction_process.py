"""Bounded POSIX child lifetime inside a registry compaction authority scope.

Internal transport, not a compaction API or a receipt verifier. The command
must be a trusted, non-forking native helper which retains the inherited
FD until exit. No runtime caller is enabled yet. The caller holds
``guard_owner_compaction`` throughout this function, journals intent BEFORE
calling it, and treats transport failure as UNKNOWN (never resends).
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
from collections.abc import Sequence


class CompactionTransportUnknownError(RuntimeError):
    """The child may have mutated; reconciliation, not replay, is required."""


def run_authority_child(
    command: Sequence[str],
    request: bytes,
    *,
    authority_fd: int,
    timeout: float,
    retained_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Run one trusted helper, retaining authority across crash/cancellation.

    Pass the descriptor number separately to the trusted helper in `command`.
    `pass_fds` alone is not an authenticated native commit protocol. This
    function must never be exposed as an agent-selectable arbitrary command.
    Output must be bounded by the helper protocol; this is not a shell runner.
    """
    if os.name != "posix":
        raise NotImplementedError("Inherited compaction authority requires POSIX flock")
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Compaction child deadline must be in (0, 30] seconds")
    inherited = tuple(dict.fromkeys((authority_fd, *retained_fds)))
    for fd in inherited:
        os.fstat(fd)
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=inherited,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(request, timeout=timeout)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise CompactionTransportUnknownError(
            "Native commit transport UNKNOWN; never replay"
        ) from error
    finally:
        # Covers BaseException (including cancellation/KeyboardInterrupt), not
        # only timeout. Never leave a running direct child when returning the
        # authority scope. On parent SIGKILL the kernel retains flock in the
        # child instead; no explicit LOCK_UN may release that inherited lock.
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
