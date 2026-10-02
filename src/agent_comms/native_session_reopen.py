"""Read-only strict native session validation before a discarded idle Pi reopens.

The pinned EntryStore owns strict v3 parsing and the observed file revision;
preflight does not construct a session writer or activate its consumers.
"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .native_package import verify_native_package
from .pi_helper import PiHelper, SessionHelperRequest
from .private_path import FileRevision


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

    @property
    def path(self) -> Path:
        return Path(self.session_file)

    def require_same_session(self, other: NativeSessionIdentity) -> None:
        if not self.same_session(other):
            raise NativeReopenError("Native saved source identity changed")

    def require_context(self, context) -> None:
        self.require_same_session(NativeSessionIdentity(context.session_id, str(context.session_file)))

    def same_session(self, other: NativeSessionIdentity) -> bool:
        """A cutpoint may extend this identity without changing its meaning."""
        return self.session_id == other.session_id and self.session_file == other.session_file

    def require_session(self, canonical: str) -> None:
        if self.session_file != canonical:
            raise ValueError("Native identity differs from owner's canonical session")


class ReopenSessionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "reopen_session.mjs"
    request = SessionHelperRequest
    result = NativeSessionIdentity


def validate_native_reopen(
    package: Path, session_file: str, *, expected_session_id: str | None = None
) -> NativeSessionIdentity:
    """Return the strict saved session identity; never mutate/recover an invalid file."""
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
            or before.st_size <= 0
        ):
            raise NativeReopenError("Saved native session is not a regular file")
        identity = asyncio.run(
            ReopenSessionHelper.run(SessionHelperRequest(str(package), str(file)), cwd=file.parent)
        )
        after = file.stat()

        if FileRevision.from_stat(before) != FileRevision.from_stat(after):
            raise NativeReopenError("Saved native session validation failed or changed")
        if identity.session_file != str(file) or (
            expected_session_id is not None and identity.session_id != expected_session_id
        ):
            raise NativeReopenError("Saved native session identity changed")
        return identity
    except (OSError, ValueError) as error:
        if isinstance(error, NativeReopenError):
            raise
        raise NativeReopenError("Saved native session cannot be validated") from error
