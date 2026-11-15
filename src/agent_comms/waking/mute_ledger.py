"""Per-member wake mutes: a member silences their own wake for a bounded time.

Wake-all is the default: every channel member is woken to triage a post (#3478). A mute
is the member's own opt-out — while it is active the member still RECEIVES every message,
only their wake is suppressed, so the group chat still pings everybody unless someone has
chosen quiet. One small durable file, read once per publish.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.storage.locked_store import DocumentChange, DurableFile, LockedStore
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore


@dataclass(frozen=True, slots=True)
class Mute:
    """One member's wake silenced until ``until`` (epoch seconds)."""

    muter: ThreadLookup
    until: float

    def active(self, now: float) -> bool:
        return self.until > now


@dataclass(frozen=True, slots=True)
class MuteLedger(SingleFileStore, DurableFile, LockedStore[tuple[Mute, ...]], DependentStateTier,
                 declared_name="mute_ledger"):
    """Every member's active wake mute; the declarations supply the stored shape via FieldCodec."""

    filename: ClassVar[str] = "mutes.json"
    record_type = tuple[Mute, ...]

    def empty(self) -> tuple[Mute, ...]:
        return ()

    def muted(self, now: float) -> frozenset[ThreadLookup]:
        """The lookups whose wake is suppressed right now; expired mutes fall out by time alone."""
        return frozenset(mute.muter for mute in self.read() if mute.active(now))

    def set(self, muter: ThreadLookup, until: float) -> None:
        self.update(lambda mutes: DocumentChange.to(
            (*(mute for mute in mutes if mute.muter != muter), Mute(muter, until))))

    def clear(self, muter: ThreadLookup) -> None:
        self.update(lambda mutes: DocumentChange.to(
            tuple(mute for mute in mutes if mute.muter != muter)))
