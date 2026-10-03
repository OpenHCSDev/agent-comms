"""Read-only saved identity selection; native startup owns strict history loading."""

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

    @staticmethod
    def locate(package: Path, session_file: str) -> NativeSessionIdentity:
        """Acquire a verified package before a standalone saved-header lookup."""
        try:
            verify_native_package(package)
            return SessionIdentityHelper.locate(package, session_file)
        except (OSError, ValueError) as error:
            if isinstance(error, NativeReopenError):
                raise
            raise NativeReopenError("Saved native session identity cannot be read") from error


class SessionIdentityHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "session_identity.mjs"
    request = SessionHelperRequest
    result = NativeSessionIdentity

    @classmethod
    def locate(cls, package: Path, session_file: str) -> NativeSessionIdentity:
        """Locate the original header without constructing another history index.

        A located identity is not history readiness. The actual native loader
        validates the whole journal before get_state; custody compares that
        attestation with this identity before any input can be granted.
        """
        try:
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
                cls.run(
                    SessionHelperRequest(str(package), str(file)), cwd=file.parent
                )
            )
            if FileRevision.from_stat(before) != FileRevision.from_stat(file.stat()):
                raise NativeReopenError("Saved native session identity changed during read")
            identity.require_session(str(file))
            return identity
        except (OSError, ValueError) as error:
            if isinstance(error, NativeReopenError):
                raise
            raise NativeReopenError("Saved native session identity cannot be read") from error
