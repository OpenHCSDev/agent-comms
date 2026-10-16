"""Recorded thread provenance, without process, turn or admission authority."""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from .channel_targets import Tag
from .field_codec import FieldCodec, FieldRepresentation
from .thread_identity import (
    DeletedThreadIncarnation, RecordedThreadIncarnation, ThreadIncarnation,
    ThreadIncarnationRecord, UnrecordedThreadIncarnation,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ThreadProvenance:
    name: str
    tags: frozenset[str]
    worktree: str
    created_at: float = field(metadata={"wire_required": True})
    _incarnation: ThreadIncarnation = field(init=False, repr=False, compare=False)
    session_file: str | None = None
    title: str | None = None

    def make_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.name, self.created_at)

    @property
    def provenance_type(self) -> type[ThreadProvenance]:
        return ThreadProvenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "_incarnation", self.make_incarnation())
        if self._incarnation.created_at != self.created_at:
            raise ValueError("Thread provenance birth differs from its declared state")
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
        return thread.provenance_type(
            **{
                item.name: getattr(thread, item.name)
                for item in fields(ThreadProvenance)
                if item.init
            }
        )

    @property
    def incarnation(self) -> ThreadIncarnation:
        return self._incarnation


@dataclass(frozen=True, slots=True, kw_only=True)
class UnrecordedThreadProvenance(ThreadProvenance):
    def make_incarnation(self) -> ThreadIncarnation:
        return UnrecordedThreadIncarnation(self.name)

    @property
    def provenance_type(self) -> type[ThreadProvenance]:
        return UnrecordedThreadProvenance


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletedThreadProvenance(ThreadProvenance):
    def make_incarnation(self) -> ThreadIncarnation:
        return DeletedThreadIncarnation(self.name)

    @property
    def provenance_type(self) -> type[ThreadProvenance]:
        return DeletedThreadProvenance


class ThreadProvenanceRecord(FieldRepresentation):
    """Decode an archived registry row's recorded birth state exactly once."""

    @classmethod
    def encode(cls, value: ThreadProvenance) -> object:
        return FieldCodec.encode(value)

    @classmethod
    def schema(cls) -> dict:
        return FieldCodec.record_schema(ThreadProvenance)

    @classmethod
    def decode(cls, value: object) -> ThreadProvenance:
        if not isinstance(value, dict):
            raise TypeError("Recorded thread provenance must be an object")
        identity = ThreadIncarnationRecord.decode(value["name"], value["created_at"])
        return FieldCodec.decode(identity.provenance_type(), value)
