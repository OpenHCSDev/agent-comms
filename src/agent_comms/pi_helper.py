"""Packaged Pi programs: A12 lifetime plus A2 request/result boundaries."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .child_process import BoundedRun, ChildResult
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord
from .launch_runtime import INHERITED_ENVIRONMENT, LaunchRuntime, RestartEnvironment
from .native_package import VerifiedNativePackage


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

    @property
    def launch_runtime(self) -> LaunchRuntime:
        return LaunchRuntime.running()


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
    def launch(cls, request, environment: dict[str, str]):
        return cls.node_launch(Path(request.package), environment)

    @classmethod
    def node_launch(cls, package: Path, environment: dict[str, str]):
        node = shutil.which("node")
        if node is None:
            raise PiHelperError("Pi helper needs Node")
        configuration = RestartEnvironment.inherit(environment)
        environment = {**VerifiedNativePackage.node_environment(
            {**environment, **configuration.encode_native()}), "PI_OFFLINE": "1"}
        command = (node, "--no-global-search-paths")
        # Every current caller supplies the verified native deployment.
        fence = package / "dist/agent-comms-import-fence.mjs"
        command += ("--import", str(fence))
        return (*command, "--input-type=module", "--eval", cls.script.read_text()), environment

    @classmethod
    async def run(cls, request, *, cwd: Path, env: Mapping[str, str] = INHERITED_ENVIRONMENT):
        command, environment = cls.launch(request, dict(env))
        payload = FieldCodec.compact(FieldCodec.encode(request), allow_nan=False)
        outcome = await BoundedRun.run(
            command,
            input=payload.encode(),
            timeout=cls.timeout_for(request),
            cwd=cwd,
            env=environment,
            runtime=request.launch_runtime,
        )
        if not outcome.outcome.successful:
            raise cls.failure(request, outcome)
        if len(outcome.stdout) > cls.output_bound:
            raise PiHelperOutputExceeded(
                f"{cls.declared_name} wrote {len(outcome.stdout)} bytes; bound is {cls.output_bound}"
            )
        try:
            return FieldCodec.decode(cls.result, FieldCodec.parse(outcome.stdout))
        except MalformedRecord as error:
            raise PiHelperError(f"{cls.declared_name} returned invalid evidence") from error
