"""Bound cold native initialization across owners; never hold a slot across a turn."""

from __future__ import annotations

import asyncio
import errno
import getpass
import os
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .session_fence import _open_lock, _try_lock, _unlock

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession
    from .native_pi import NativePiRpcLaunch
    from .pi_payloads import StateData
    from .native_session_reopen import NativeSessionIdentity


@dataclass(frozen=True)
class NativeStartupPolicy:
    slots: int = 4
    # A cold extension can initialize a local stdio server before Pi answers get_state.
    readiness_seconds: float = 10.0
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
        Missing/unreadable sessions retain the ten-second base bound.
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

    @classmethod
    def for_launch(
        cls, launch: NativePiRpcLaunch, *, root: Path | None = None,
    ) -> NativeStartupAdmission:
        if root is not None:
            return cls(root)
        return cls(
            Path(
                launch.env.get("AGENT_COMMS_ROOT")
                or os.environ.get("AGENT_COMMS_ROOT")
                or str(Path(tempfile.gettempdir()) / f"agent-comms-startup-{getpass.getuser()}")
            ).expanduser()
        )

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

    def attest(self, state: StateData) -> None:
        """Ordinary startup has no fresh selected-source enrollment to attest."""

    def prompt_boundary(
        self, delegate: Callable[..., AbstractContextManager[None]], identity: NativeSessionIdentity,
    ) -> AbstractContextManager[None]:
        return delegate(identity)


class SelectedNativeStartupAdmission(NativeStartupAdmission):
    """Custody of one original fresh enrollment and its exact startup revision."""

    def __init__(
        self, root: Path | None, fresh: FreshPrivateSession,
        prompt_send_boundary: Callable[..., AbstractContextManager[None]] | None,
    ):
        from .fresh_private_session import FreshPrivateSession
        from .native_pi import (
            NativePiUnavailable, _fresh_selected_revision,
            _require_reviewed_selected_source_cli,
        )
        from .pi_vocabulary import ThinkingLevel

        if type(fresh) is not FreshPrivateSession:
            raise NativePiUnavailable("Selected startup requires original fresh enrollment")
        if root is None or prompt_send_boundary is None:
            raise NativePiUnavailable("Selected first source requires enrolled locked prewrite")
        if not ThinkingLevel.supports_selected(fresh.selected_thinking_level):
            raise NativePiUnavailable("Selected startup lacks an explicit supported thinking level")
        revision = _fresh_selected_revision(fresh)
        _require_reviewed_selected_source_cli()
        super().__init__(root)
        self.fresh = fresh
        self.revision = revision

    def attest(self, state: StateData) -> None:
        from .native_pi import NativePiUnavailable, _fresh_selected_revision

        self.fresh.require_runtime(state)
        revision = _fresh_selected_revision(self.fresh, started=True)
        if revision.identity != self.revision.identity:
            raise NativePiUnavailable("Selected startup changed enrolled inode")
        self.revision = revision

    def prompt_boundary(
        self, delegate: Callable[..., AbstractContextManager[None]], identity: NativeSessionIdentity,
    ) -> AbstractContextManager[None]:
        return delegate(identity, self.revision)
