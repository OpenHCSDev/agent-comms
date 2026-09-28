"""Read Pi's effective compaction settings without its mutable settings storage.

This is trigger evidence only, not owner authority, source capture, or a
provider request. A future ACP caller must bind the selected model/window and
recheck its source before a native commit; nothing invokes this automatically.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .field_codec import FieldCodec
from .native_package import verify_native_package

_READ_SETTINGS_SCRIPT = Path(__file__).with_name("_pi_helpers") / "compaction_settings.mjs"


class PiSettingsEvidenceError(ValueError):
    """Effective Pi settings could not be read without mutation; skip trigger."""


@dataclass(frozen=True)
class PiCompactionSettings:
    """Pi's compaction budget, decoded by A2 once with application bounds."""

    reserve_tokens: int = field(metadata={"wire_name": "reserveTokens"})
    keep_recent_tokens: int = field(metadata={"wire_name": "keepRecentTokens"})

    def __post_init__(self):
        if (
            not 0 <= self.reserve_tokens <= 10_000_000
            or not 0 < self.keep_recent_tokens <= 10_000_000
        ):
            raise PiSettingsEvidenceError("Invalid effective Pi compaction settings")


@dataclass(frozen=True)
class PiCompactionDecision(PiCompactionSettings):
    """A settings observation plus Pi's enablement and trigger decision."""

    enabled: bool = field(metadata={"settings_exclude": True})
    trigger: bool = field(metadata={"settings_exclude": True})


def read_compaction_decision(
    package: Path, worktree: str, *, context_tokens: int, context_window: int
) -> PiCompactionDecision:
    """Evaluate Pi's declared trigger on bounded evidence without mutating Pi.

    This returns no grant to generate a summary or write a session. The future
    owner caller must separately capture/recheck turn, model and ingress.
    """
    if (
        type(context_tokens) is not int
        or not 0 <= context_tokens <= 2**53 - 1
        or type(context_window) is not int
        or not 0 < context_window <= 2**53 - 1
    ):
        raise PiSettingsEvidenceError("Invalid selected-model context evidence")
    try:
        package = package.resolve(strict=True)
        verify_native_package(package)
        cwd = Path(worktree).absolute()
        if cwd != cwd.resolve(strict=True) or not cwd.is_dir():
            raise PiSettingsEvidenceError("Worktree is not canonical")
        node = shutil.which("node")
        if node is None:
            raise PiSettingsEvidenceError("Pinned Pi settings reader unavailable")
        environment = dict(os.environ)
        for key in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
            environment.pop(key, None)
        environment["NODE_DISABLE_COMPILE_CACHE"] = "1"
        environment["PI_OFFLINE"] = "1"
        result = subprocess.run(
            [
                node,
                "--no-global-search-paths",
                "--import",
                str(package / "dist/agent-comms-import-fence.mjs"),
                "--input-type=module",
                "--eval",
                _READ_SETTINGS_SCRIPT.read_text(),
                str(package),
                str(cwd),
                str(context_tokens),
                str(context_window),
            ],
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=10,
        )
        if result.returncode or len(result.stdout) > 1024:
            raise PiSettingsEvidenceError("Pi settings reader refused")
        data = json.loads(result.stdout)
        return FieldCodec.decode(PiCompactionDecision, data)
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, PiSettingsEvidenceError):
            raise
        raise PiSettingsEvidenceError("Pi settings decision unavailable") from error
