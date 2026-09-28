"""Read-only strict native session validation before a discarded idle Pi reopens.

Never use SessionManager.open for this preflight: it can rewrite a saved file.
The pinned manager's loadEntriesFromFile enforces its actual strict v3 parse.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .native_package import MANIFEST, verify_native_package
from .pi_helper import PiHelper, SessionHelperRequest


class NativeReopenError(ValueError):
    """Saved session cannot be safely reloaded; no provider input may start."""


@dataclass(frozen=True)
class NativeSessionIdentity:
    """SessionManager's observed saved-session identity."""

    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})

    def __post_init__(self):
        if not self.session_id or not Path(self.session_file).is_absolute():
            raise NativeReopenError("Saved native session identity is incomplete")


class ReopenSessionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "reopen_session.mjs"
    request = SessionHelperRequest
    result = NativeSessionIdentity


def package_for_launcher(launcher: str) -> Path:
    """Bind a canonical launcher to the wheel/source-owned complete-tree pin."""
    executable = Path(shutil.which(launcher) or launcher).resolve(strict=True)
    if executable.name == "pi-comms-native":
        from .coordination_store import PublicationActivationBlocked
        from .native_pi import NativePiUnavailable
        from .private_nk_entrypoint import private_nk_from_environment

        try:
            launch = private_nk_from_environment()
        except (NativePiUnavailable, PublicationActivationBlocked) as error:
            raise NativeReopenError("Native owner route is unavailable") from error
        if launch is None:
            raise NativeReopenError("Native owner backend requires a configured private route")
        # Use the launcher's route owner, then apply the same complete-tree
        # commitment as the stack launcher. A path or seven-file probe alone
        # cannot select different preparation/commit helpers after compaction.
        verify_native_package(launch.native_package)
        return launch.native_package
    if executable.name != "pi-native" or executable.parent.name != "bin":
        raise NativeReopenError("Canonical native launcher required for saved-session reopen")
    stack = executable.parent.parent
    manifest = stack / "pi-native.sha256"
    if manifest.read_bytes() != MANIFEST.read_bytes():
        raise NativeReopenError("Launcher and Python native commitments differ")
    build = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()[:16]
    package = stack / f".pi-native-{build}" / "node_modules/@earendil-works/pi-coding-agent"
    verify_native_package(package)
    return package


def validate_native_reopen(
    launcher: str, session_file: str, *, expected_session_id: str | None = None
) -> str:
    """Return the strict saved session ID; never mutate/recover an invalid file."""
    try:
        package = package_for_launcher(launcher)
        file = Path(session_file).absolute()
        if file != file.resolve(strict=True):
            raise NativeReopenError("Saved native session path is not canonical")
        before = file.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in (0, os.getuid())
            or not 0 < before.st_size <= 256 * 1024 * 1024
        ):
            raise NativeReopenError("Saved native session is not a bounded regular file")
        identity = asyncio.run(
            ReopenSessionHelper.run(SessionHelperRequest(str(package), str(file)), cwd=file.parent)
        )
        after = file.stat()

        def revision(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if revision(before) != revision(after):
            raise NativeReopenError("Saved native session validation failed or changed")
        if identity.session_file != str(file) or (
            expected_session_id is not None and identity.session_id != expected_session_id
        ):
            raise NativeReopenError("Saved native session identity changed")
        return identity.session_id
    except (OSError, ValueError) as error:
        if isinstance(error, NativeReopenError):
            raise
        raise NativeReopenError("Saved native session cannot be validated") from error
