"""Packaged Pi programs: A12 lifetime plus A2 request/result boundaries."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .child_process import BoundedRun, ChildResult
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec


class PiHelperError(ValueError):
    """A helper produced no complete, bounded observation."""


class PiHelperFailed(PiHelperError):
    """The helper ended without success; carries its actual outcome and stderr."""

    stderr_shown: ClassVar[int] = 4096

    def __init__(self, helper: str, result: ChildResult, consequence: str = ""):
        self.helper = helper
        self.result = result
        stderr = result.stderr[-self.stderr_shown:].decode("utf-8", "backslashreplace").strip()
        super().__init__(
            f"{helper} ended {result.outcome}"
            + (f"; stderr: {stderr}" if stderr else "; no stderr")
            + (f"; {consequence}" if consequence else "")
        )


class PiHelperOutputExceeded(PiHelperError):
    """The helper succeeded but wrote more than its output bound."""


@dataclass(frozen=True)
class SessionHelperRequest:
    package: str
    file: str


class PiHelper(DeclaredFamily, affix="Helper"):
    """A helper declares only its packaged program and input/output records.

    The existing native fence intentionally refuses scripts outside its pinned
    deployment. Reading our shipped module as an eval entry retains that fence;
    imports still resolve exclusively inside the caller-verified Pi package.
    """

    script: ClassVar[Path]
    request: ClassVar[type]
    result: ClassVar[type]
    timeout_seconds: ClassVar[float] = 10
    output_bound: ClassVar[int] = 4096

    @classmethod
    def timeout_for(cls, request) -> float:
        """The helper's bound for this request; the declaration owns it."""
        return cls.timeout_seconds

    @classmethod
    def failure(cls, request, result: ChildResult) -> PiHelperFailed:
        """The error for an unsuccessful outcome; helpers add what it leaves behind."""
        return PiHelperFailed(cls.declared_name, result)

    @classmethod
    async def run(cls, request, *, cwd: Path, env: dict[str, str] | None = None):
        from .owner_launch import RestartEnvironment

        node = shutil.which("node")
        if node is None:
            raise PiHelperError("Pi helper needs Node")
        environment = dict(os.environ if env is None else env)
        configuration = RestartEnvironment.inherit(environment)
        environment.update(configuration.encode_native())
        for key in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
            environment.pop(key, None)
        environment["NODE_DISABLE_COMPILE_CACHE"] = "1"
        environment["PI_OFFLINE"] = "1"
        package = Path(request.package)
        command = (node, "--no-global-search-paths")
        # Every current caller supplies the verified native deployment.
        fence = package / "dist/agent-comms-import-fence.mjs"
        command += ("--import", str(fence))
        payload = json.dumps(FieldCodec.encode(request), separators=(",", ":"), allow_nan=False)
        outcome = await BoundedRun.run(
            (*command, "--input-type=module", "--eval", cls.script.read_text()),
            input=payload.encode(),
            timeout=cls.timeout_for(request),
            cwd=cwd,
            env=environment,
        )
        if not outcome.outcome.successful:
            raise cls.failure(request, outcome)
        if len(outcome.stdout) > cls.output_bound:
            raise PiHelperOutputExceeded(
                f"{cls.declared_name} wrote {len(outcome.stdout)} bytes; bound is {cls.output_bound}"
            )
        try:
            return FieldCodec.decode(cls.result, json.loads(outcome.stdout))
        except (TypeError, ValueError, UnicodeError) as error:
            raise PiHelperError(f"{cls.declared_name} returned invalid evidence") from error
