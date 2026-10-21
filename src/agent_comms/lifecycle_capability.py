"""Shared lifecycle behavior composed by the domain state declarations."""

from __future__ import annotations


class LifecycleCapability:
    """The neutral behavior shared by lifecycle families, without another registry."""

    @classmethod
    def require_nonatomic_advance(cls) -> None:
        """An unfinished state may be recorded before atomic settlement."""

    @classmethod
    def success_count(cls) -> int:
        """Contribution to an aggregate of successful results."""
        return 0


class Terminal(LifecycleCapability):
    """A settled state has no ordinary successor and cannot advance piecemeal."""

    @classmethod
    def successors(cls) -> tuple[type, ...]:
        return ()

    @classmethod
    def require_nonatomic_advance(cls) -> None:
        from .coordination_errors import IdentityConflict

        raise IdentityConflict("terminal phases require atomic settlement")


class Successful(Terminal):
    @classmethod
    def success_count(cls) -> int:
        return 1
