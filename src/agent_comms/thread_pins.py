"""A pin preserves either its recorded birth or explicitly unattributed old text."""
from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass

from .nominal.declared_family import DeclaredFamily
from .nominal.field_codec import ThreadLookup
from .thread_identity import (
    RecordedThreadIncarnation,
    ThreadIncarnation,
    UnrecordedThreadIncarnation,
)


class ThreadPin(DeclaredFamily, affix="ThreadPin"):
    """Consumers ask the pin for lookup capabilities and retained display evidence."""

    thread: ThreadIncarnation

    @classmethod
    def recorded(cls, thread: RecordedThreadIncarnation) -> ThreadPin:
        return RecordedThreadPin(thread)

    @classmethod
    def unattributed(cls, name: str) -> ThreadPin:
        return UnattributedThreadPin(UnrecordedThreadIncarnation(name))

    @property
    def sort_key(self) -> tuple[str, float]:
        return self.thread.name, self.thread.wire_birth

    def lookups(self) -> tuple[ThreadLookup, ...]:
        return ()

    def notices(self, present: Container[ThreadLookup]) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class RecordedThreadPin(ThreadPin):
    thread: RecordedThreadIncarnation

    def __post_init__(self) -> None:
        self.thread.require_recorded()

    def lookups(self) -> tuple[ThreadLookup, ...]:
        return (self.thread.lookup,)

    def notices(self, present: Container[ThreadLookup]) -> tuple[str, ...]:
        return tuple(
            f"last known pin: {self.thread.name} (owner absent from this registry)"
            for lookup in self.lookups() if lookup not in present
        )


@dataclass(frozen=True)
class UnattributedThreadPin(ThreadPin):
    thread: UnrecordedThreadIncarnation

    def __post_init__(self) -> None:
        # A custom incarnation wire decoder must not widen this historical member.
        if self.thread.wire_birth != UnrecordedThreadIncarnation.WIRE_BIRTH:
            raise ValueError("An unattributed pin cannot claim a recorded birth")

    def notices(self, present: Container[ThreadLookup]) -> tuple[str, ...]:
        return (f"last known pin: {self.thread.name} (owner unrecorded)",)
