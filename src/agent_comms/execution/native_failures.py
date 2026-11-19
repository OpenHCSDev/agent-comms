"""Native request failures, independent of launch and filesystem acquisitions."""

from __future__ import annotations

from collections.abc import Callable

from agent_comms.coordination.errors import DrainDeferred
from agent_comms.execution.failure_source import FailureSource, UnrecordedFailureSource

class NativeRequestFailure(RuntimeError, DrainDeferred):  # noqa: N818 - nominal request outcome
    """A failed input is reported and settled without authorizing a retry."""

    native_source: FailureSource = UnrecordedFailureSource()

    def diagnostic_evidence(self) -> dict[str, object]:
        return self.native_source.diagnostic_evidence()

    def attach_stderr(self, text: str) -> None:
        pass

    @property
    def public_failure(self) -> str:
        return str(self)

    def publish[Result](self, publisher: Callable[..., Result]) -> Result:
        """An ordinary failure has no rejected native response to publish."""
        return publisher(self.public_failure, source_error=self)


class NativePiUnavailable(NativeRequestFailure):  # noqa: N818 - nominal fail-closed outcome
    """Tracked execution failed closed without committing a coordinator fact."""

    native_stderr = ""

    def diagnostic_evidence(self) -> dict[str, object]:
        return {**super().diagnostic_evidence(), "stderr": self.native_stderr}

    def attach_stderr(self, text: str) -> None:
        self.native_stderr = text

    @property
    def public_failure(self):
        return f"{self}; the input is uncertain."
