"""Asking another program on this machine for an answer, under a bound.

Both tailnet probes ask a program and read what it says: which machines hold a bus, and
what this machine serves. The timeout and the read of both streams are one fact, declared
once here, because the two copies had already drifted on it (12 seconds against 10) before
either of them shipped.

`BoundedRun` in child_process.py owns the asynchronous case, where a child is attached,
supervised and retired. These probes are synchronous, called from a worker thread, and
want nothing supervised: a program runs, says something, and ends.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import ClassVar


class ProbedCommand:
    """Something whose answers come from another program's output."""

    timeout_seconds: ClassVar[float] = 10
    """How long one call may take before the probe gives up; a member that asks across the
    network declares its own."""

    @classmethod
    def run(cls, command: Sequence[str]) -> tuple[int, str]:
        """Its status and answer; a failure retains both output and diagnostics.

        A refusal comes back as a status and its own words, never as an exception: every
        caller here has a member for "it would not answer" and needs the reason in it.
        """
        try:
            done = subprocess.run(tuple(command), capture_output=True, text=True, errors="replace",
                                  timeout=cls.timeout_seconds * 2, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return 1, str(error)
        return done.returncode, (done.stdout if done.returncode == 0
                                 else done.stdout + done.stderr)
