"""Native SessionManager owns the fork snapshot before a child is registered."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .native_session_reopen import NativeSessionIdentity
from .pi_helper import PiHelper, SessionHelperRequest


@dataclass(frozen=True)
class ForkSessionRequest(SessionHelperRequest):
    cwd: str
    directory: str | None = None


class ForkSessionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "fork_session.mjs"
    request = ForkSessionRequest
    result = NativeSessionIdentity


def fork_native_session(file: str, worktree: str, launcher: str) -> NativeSessionIdentity:
    """Capture once under the caller's wire lock and native writer's source lock.

    The native owner fsyncs the new history. An uncertain helper outcome is not
    retried or inferred from an orphan file, and no input is sent.
    """
    from .native_pi import NativePiRpcLaunch

    package = NativePiRpcLaunch.package_for_command(launcher)
    return asyncio.run(
        ForkSessionHelper.run(
            ForkSessionRequest(str(package), file, worktree),
            cwd=Path(worktree),
        )
    )
