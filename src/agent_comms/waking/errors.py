from __future__ import annotations


class ProjectionUnavailableError(RuntimeError):
    """Original delivery remains available when the optional projection fails."""
