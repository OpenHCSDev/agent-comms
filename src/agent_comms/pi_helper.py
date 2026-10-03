"""Packaged Pi programs: A12 lifetime plus A2 request/result boundaries."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .child_process import BoundedRun
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec


class PiHelperError(ValueError):
    """A helper produced no complete, bounded observation."""


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
            timeout=cls.timeout_seconds,
            cwd=cwd,
            env=environment,
        )
        if not outcome.outcome.successful or len(outcome.stdout) > 4096:
            raise PiHelperError(f"{cls.declared_name} failed or exceeded its output bound")
        try:
            return FieldCodec.decode(cls.result, json.loads(outcome.stdout))
        except (TypeError, ValueError, UnicodeError) as error:
            raise PiHelperError(f"{cls.declared_name} returned invalid evidence") from error
