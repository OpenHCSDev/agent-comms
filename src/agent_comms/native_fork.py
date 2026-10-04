"""Native SessionManager owns the fork snapshot before a child is registered."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .native_session_reopen import NativeSessionIdentity
from .pi_helper import PiHelper, SessionHelperRequest
from .compaction_records import NativeForkCreation


@dataclass(frozen=True)
class ForkSessionRequest(SessionHelperRequest):
    cwd: str
    directory: str | None = None
    creation_kind: type[NativeForkCreation] = field(default=NativeForkCreation, kw_only=True)


class ForkSessionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "fork_session.mjs"
    request = ForkSessionRequest
    result = NativeForkCreation


def fork_native_session(file: str, worktree: str, launcher: str, *, private_inputs) -> NativeSessionIdentity:
    """Capture once under the caller's wire lock and native writer's source lock.

    The native owner fsyncs the new history. An uncertain helper outcome is not
    retried or inferred from an orphan file, and no input is sent.
    """
    from .native_pi import NativePiRpcLaunch

    package = NativePiRpcLaunch.package_for_command(launcher)
    return asyncio.run(
        private_inputs.fork(
            ForkSessionRequest(str(package), file, worktree),
            cwd=Path(worktree),
        )
    )
