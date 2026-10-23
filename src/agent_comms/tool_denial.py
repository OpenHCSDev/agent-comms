"""The refusal every selected or native tool path raises, below the modules that raise it."""

from __future__ import annotations


class SelectedToolDenied(ValueError):  # noqa: N818 - nominal fail-closed outcome
    """A rejected request, including duplicate or uncertain prior consumption."""
