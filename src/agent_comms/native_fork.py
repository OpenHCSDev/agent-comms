"""Native SessionManager owns the fork snapshot before a child is registered."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .child_process import ChildResult, SignaledOutcome, TimedOutOutcome
from .native_session_reopen import NativeSessionIdentity
from .pi_helper import PiHelper, PiHelperFailed, SessionHelperRequest


@dataclass(frozen=True)
class ForkSessionRequest(SessionHelperRequest):
    cwd: str
    directory: str | None = None


class ForkSessionHelper(PiHelper):
    """Copies the parent's whole saved history under both native writer locks.

    The native package never steals a writer lock, so killing this helper
    mid-copy leaves the parent's lock (and a partial child file) until a
    person reviews them. Its bound therefore grows with the parent: forking
    a 147 MB history took 5.5 s on an idle host (2026-10-09), and the bound
    allows a several-fold slower host before it kills a writer.
    """

    script = Path(__file__).with_name("_pi_helpers") / "fork_session.mjs"
    request = ForkSessionRequest
    result = NativeSessionIdentity
    copy_bytes_per_second: ClassVar[float] = 4 * 1024 * 1024

    @classmethod
    def timeout_for(cls, request: ForkSessionRequest) -> float:
        return cls.timeout_seconds + Path(request.file).stat().st_size / cls.copy_bytes_per_second

    @classmethod
    def failure(cls, request: ForkSessionRequest, result: ChildResult) -> PiHelperFailed:
        # A thrown native error releases both locks in its finally; only a
        # signal (our bound's stop, or an external kill) skips that release.
        if not isinstance(result.outcome, (TimedOutOutcome, SignaledOutcome)):
            return super().failure(request, result)
        return PiHelperFailed(
            cls.declared_name,
            result,
            f"killed (bound {cls.timeout_for(request):.0f}s) while it may hold the native "
            f"writer locks of {request.file}; that lock and any partial fork in "
            f"{request.directory or 'the default session directory for ' + request.cwd} "
            "remain and block later forks of this parent until reviewed (locks are never "
            "stolen automatically)",
        )


def fork_native_session(file: str, worktree: str, launcher: str) -> NativeSessionIdentity:
    """Copy the parent's history once under the native writer locks, before any wire lock.

    The native owner fsyncs the new history. An uncertain helper outcome is not
    retried or inferred from an orphan file, and no input is sent.
    """
    from .native_pi import NativePiRpcLaunch

    package = NativePiRpcLaunch.package_for_command(launcher)
    return asyncio.run(ForkSessionHelper.run(
        ForkSessionRequest(str(package), file, worktree), cwd=Path(worktree),
    ))
