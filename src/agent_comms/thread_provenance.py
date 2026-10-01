"""Recorded thread provenance, without process, turn or admission authority."""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from .channel_targets import Tag
from .thread_identity import ThreadIncarnation


@dataclass(frozen=True, slots=True, kw_only=True)
class ThreadProvenance:
    name: str
    tags: frozenset[str]
    worktree: str
    created_at: float = field(metadata={"wire_required": True})
    session_file: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        # Historical evidence retains finite recorded values, including zero.
        # Positive-birth admission belongs to the existing live capability.
        self.incarnation
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not self.name or not set(self.name) <= allowed:
            raise ValueError(f"Thread name {self.name!r} must be alphanumeric with hyphens/underscores.")
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")
        for tag in self.tags:
            Tag(tag)

    @staticmethod
    def capture(thread: ThreadProvenance) -> ThreadProvenance:
        """Freeze only determining provenance from the original typed declaration."""
        return ThreadProvenance(
            **{item.name: getattr(thread, item.name) for item in fields(ThreadProvenance)}
        )

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.name, self.created_at)
