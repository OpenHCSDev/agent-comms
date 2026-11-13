"""What a context writer knows about its thread's previous request, and the store it reads
that request from. Both the store and a Pi process's published context import this."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass

from agent_comms.execution.turn_context import (
    ContextEntryManifest,
    ContextManifest,
    ContextRecording,
    RecordedContextManifest,
)
from agent_comms.nominal.field_codec import ContextStoreId
from agent_comms.registry.thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class ContextStoreIdentity:
    value: ContextStoreId


class ReadsContextPrior(ABC):
    """The store a context writer reads its thread's previous request from."""

    @abstractmethod
    def prior(self, owner: ThreadIncarnation) -> RecordedContextState: ...


class ContextPrior(ABC):
    """What a context writer knows about its thread's previous request."""

    @abstractmethod
    def read(self, store: ReadsContextPrior, owner: ThreadIncarnation) -> RecordedContextState: ...


class UnreadContextPrior(ContextPrior):
    """A writer that has not yet read its thread's previous request."""

    def read(self, store: ReadsContextPrior, owner: ThreadIncarnation) -> RecordedContextState:
        return store.prior(owner)


UNREAD_CONTEXT = UnreadContextPrior()


class RecordedContextState(ContextPrior, ContextRecording):
    """The previous request, including the first request's lack of a predecessor."""

    def read(self, store: ReadsContextPrior, owner: ThreadIncarnation) -> RecordedContextState:
        return self

    @abstractmethod
    def require_newest(
        self, db: sqlite3.Connection, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> None:
        """Refuse when another writer recorded this thread after this prior."""

    def capture(
        self, manifest: ContextManifest
    ) -> tuple[RecordedContextManifest, tuple[ContextEntryManifest, ...]]:
        return RecordedContextManifest.capture(manifest, self)
