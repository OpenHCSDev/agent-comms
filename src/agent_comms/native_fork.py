"""Native SessionManager owns the fork snapshot before a child is registered."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .child_process import ChildResult, SignaledOutcome, TimedOutOutcome
from .launch_runtime import LaunchRuntime
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


@dataclass(frozen=True, kw_only=True)
class RuntimeForkRequest(ForkSessionRequest):
    runtime: LaunchRuntime = field(metadata={"wire_exclude": True})


    @property
    def launch_runtime(self) -> LaunchRuntime:
        return self.runtime


class RuntimeForkHelper(ForkSessionHelper):
    @classmethod
    def launch(cls, request, environment):
        return ((str(request.runtime.interpreter), "-I", "-c",
                 "from agent_comms.native_fork import main; main()", request.package),
                request.runtime.environment(environment))


def main():
    command, environment = ForkSessionHelper.node_launch(Path(sys.argv[1]), dict(os.environ))
    os.execvpe(command[0], command, environment)
