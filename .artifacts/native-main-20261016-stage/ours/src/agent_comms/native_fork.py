"""Native SessionManager owns the fork snapshot before a child is registered."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, TYPE_CHECKING

from .child_process import ChildResult, SignaledOutcome, TimedOutOutcome
from .native_session_reopen import NativeSessionIdentity
from .field_codec import FieldCodec, PathText
from .native_package import verify_native_package
from .launch_runtime import LaunchRuntime
from .pi_helper import PiHelper, PiHelperFailed, RuntimeHelper, SessionHelperRequest

if TYPE_CHECKING:
    from .comms import Comms


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


@dataclass(frozen=True)
class RuntimeForkRequest:
    file: str
    cwd: str
    runtime: LaunchRuntime
    package: Annotated[Path, PathText]
    directory: str | None = None

    async def copy(self) -> NativeSessionIdentity:
        verify_native_package(self.package)
        return await ForkSessionHelper.run(
            ForkSessionRequest(str(self.package), self.file, self.cwd, self.directory),
            cwd=Path(self.cwd),
        )


class RuntimeForkHelper(RuntimeHelper, ForkSessionHelper):
    """The fork's selected Python loads its own copy implementation and SDK."""

    module = "agent_comms.native_fork"
    request = RuntimeForkRequest


async def fork_native_session(
    file: str, worktree: str, comms: Comms,
) -> NativeSessionIdentity:
    """Copy once in the child's installation before entering a wire lock.

    A failed helper may have copied bytes or held a native writer lock. Preserve
    that result and the files; never infer success or repeat the request.
    """
    request = RuntimeForkRequest(file, worktree, comms.runtime, comms.owners.native_package)
    return await RuntimeForkHelper.run(request, cwd=Path(worktree))


def main() -> int:
    request = FieldCodec.decode(RuntimeForkRequest, json.load(sys.stdin))
    result = asyncio.run(request.copy())
    print(json.dumps(FieldCodec.encode(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
