"""Bound cold native initialization across owners; never hold a slot across a turn."""

from __future__ import annotations

import asyncio
import errno
import os
from dataclasses import dataclass
from pathlib import Path

from .session_fence import _open_lock, _try_lock, _unlock


@dataclass(frozen=True)
class NativeStartupPolicy:
    slots: int = 4
    readiness_seconds: float = 5.0
    readiness_step_bytes: int = 8 * 1024 * 1024
    readiness_step_seconds: float = 2.0
    readiness_max_seconds: float = 35.0
    poll_seconds: float = 0.025

    def readiness_timeout(
        self, session_bytes: int | None, *, base_seconds: float | None = None
    ) -> float:
        """Bound get_state wait by saved-session size, not the model/turn clock.

        A cold Pi parses the entire saved JSONL before responding. Allow two
        extra seconds for each full 8 MiB beyond the first 8 MiB, up to 35s.
        Missing/unreadable sessions retain the original five-second bound.
        """
        base = self.readiness_seconds if base_seconds is None else base_seconds
        extra_steps = (
            max(0, (session_bytes - 1) // self.readiness_step_bytes)
            if session_bytes is not None and session_bytes > 0
            else 0
        )
        return min(self.readiness_max_seconds, base + extra_steps * self.readiness_step_seconds)


NATIVE_STARTUP_POLICY = NativeStartupPolicy()


class NativeStartupAdmission:
    """OS lock leases disappear on crash; no PID roster or persisted busy counter."""

    def __init__(self, root: Path, policy: NativeStartupPolicy = NATIVE_STARTUP_POLICY):
        self.directory = root / "runtime" / "native-startup"
        self.policy = policy
        self.fd: int | None = None

    async def acquire(self, finish_event: asyncio.Event | None = None) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        while True:
            if finish_event is not None and finish_event.is_set():
                raise asyncio.CancelledError
            for slot in range(self.policy.slots):
                fd = _open_lock(self.directory / f"{slot}.lock")
                try:
                    _try_lock(fd)
                except BaseException as error:
                    os.close(fd)
                    if not isinstance(error, OSError) or error.errno not in {
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    }:
                        raise
                else:
                    self.fd = fd
                    return
            await asyncio.sleep(self.policy.poll_seconds)

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                _unlock(fd)
            finally:
                os.close(fd)
