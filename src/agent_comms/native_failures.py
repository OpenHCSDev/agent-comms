"""Native request failures, independent of launch and filesystem acquisitions."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .send_result import SendResult


class NativeRequestFailure(RuntimeError):  # noqa: N818 - nominal request outcome
    """A failed input is reported and settled without authorizing a retry."""

    @property
    def public_failure(self) -> str:
        return str(self)

    def publish(self, publisher: Callable[..., SendResult]) -> SendResult:
        """An ordinary failure has no rejected native response to publish."""
        return publisher(self.public_failure, source_error=self)


class NativePiUnavailable(NativeRequestFailure):  # noqa: N818 - nominal fail-closed outcome
    """Tracked execution failed closed without committing a coordinator fact."""

    native_stderr = ""

    @property
    def diagnostic_evidence(self):
        return {"stderr": self.native_stderr}

    @property
    def public_failure(self):
        return f"{self}; the input is uncertain."
