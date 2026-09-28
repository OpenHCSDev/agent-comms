"""Read-only strict native session validation before a discarded idle Pi reopens.

Never use SessionManager.open for this preflight: it can rewrite a saved file.
The pinned manager's loadEntriesFromFile enforces its actual strict v3 parse.
"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .native_package import verify_native_package
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


def validate_native_reopen(
    package: Path, session_file: str, *, expected_session_id: str | None = None
) -> str:
    """Return the strict saved session ID; never mutate/recover an invalid file."""
    try:
        verify_native_package(package)
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
