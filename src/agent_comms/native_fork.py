"""Native SessionManager owns the fork snapshot before a child is registered."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .native_pi import NativePiRpcLaunch
from .native_session_reopen import NativeSessionIdentity
from .pi_helper import PiHelper, SessionHelperRequest
from .threads import Thread


@dataclass(frozen=True)
class ForkSessionRequest(SessionHelperRequest):
    cwd: str


class ForkSessionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "fork_session.mjs"
    request = ForkSessionRequest
    result = NativeSessionIdentity


def fork_native_session(parent: Thread, launcher: str) -> NativeSessionIdentity:
    """Capture once under the caller's wire lock and native writer's source lock.

    The native owner fsyncs the new history. An uncertain helper outcome is not
    retried or inferred from an orphan file, and no input is sent.
    """
    if parent.session_file is None:
        raise ValueError("Native fork requires a saved parent session")
    package = NativePiRpcLaunch.package_for_command(launcher)
    return asyncio.run(
        ForkSessionHelper.run(
            ForkSessionRequest(str(package), parent.session_file, parent.worktree),
            cwd=Path(parent.worktree),
        )
    )
