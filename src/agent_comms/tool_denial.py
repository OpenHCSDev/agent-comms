"""The refusal every selected or native tool path raises, below the modules that raise it."""

from __future__ import annotations

from agent_comms_hooks import ToolRefusedError


class SelectedToolDenied(ToolRefusedError):  # noqa: N818 - nominal fail-closed outcome
    """A rejected request, including duplicate or uncertain prior consumption."""


class StorageDenied(SelectedToolDenied, ToolRefusedError):  # noqa: N818 - existing nominal refusal
    """The storage preflight refused an operation before it executed, or found no room for it."""
