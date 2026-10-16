"""Native execution failures shared by launch, source selection and recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import pi_events as pi


class NativePiUnavailable(RuntimeError):  # noqa: N818 - nominal fail-closed outcome
    """Tracked execution failed closed without committing a coordinator fact."""

    native_stderr = ""

    @property
    def diagnostic_evidence(self):
        return {"stderr": self.native_stderr}

    @property
    def public_failure(self):
        return f"{self}; the input is uncertain."

    @property
    def rejected_response(self) -> pi.Response | None:
        return None
