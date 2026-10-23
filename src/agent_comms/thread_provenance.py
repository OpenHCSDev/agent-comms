"""A recorded thread declaration, independent of live execution authority."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from functools import cached_property
from typing import Annotated

from .agent_backend import AgentBackend, NewThreadBackend, RuntimeLocation
from .channel_targets import Tag
from .field_codec import WireOmitDefault, WireRequired
from .thread_identity import ThreadIncarnation, ThreadIncarnationRecord
from .thread_text import NoThreadText, ThreadText
from .session_file import NoSessionFile, SessionFile


@dataclass(frozen=True, kw_only=True)
class ThreadProvenance:
    """Recorded thread identity, without permission to own a process or start a turn."""

    name: str
    tags: frozenset[str]
    worktree: str
    created_at: Annotated[float, WireRequired()]
    session_file: SessionFile = field(default_factory=NoSessionFile)
    title: ThreadText = field(default_factory=NoThreadText)
    backend: Annotated[type[AgentBackend], WireOmitDefault()] = field(
        default_factory=NewThreadBackend.selected
    )

    @cached_property
    def runtime(self) -> RuntimeLocation:
        """The codec decoded both stored fields; their record owns this binding."""
        return self.session_file.location(self.backend)

    @property
    def display_title(self) -> str:
        return self.title.otherwise(self.name)

    @staticmethod
    def require_name(name: str) -> None:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not name or not set(name) <= allowed:
            raise ValueError(f"Thread name {name!r} must be alphanumeric with hyphens/underscores.")

    def __post_init__(self) -> None:
        self.incarnation
        self.require_name(self.name)
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")
        for tag in self.tags:
            Tag.validate(tag)

    @staticmethod
    def capture(thread: ThreadProvenance) -> ThreadProvenance:
        """Freeze only determining provenance from the original typed declaration."""
        return ThreadProvenance(
            **{
                item.name: getattr(thread, item.name)
                for item in fields(ThreadProvenance)
                if item.init
            }
        )

    @cached_property
    def incarnation(self) -> ThreadIncarnation:
        """The declaration's birth member, created once and shared by its readers."""
        return ThreadIncarnationRecord(self.name, self.created_at).incarnation()
