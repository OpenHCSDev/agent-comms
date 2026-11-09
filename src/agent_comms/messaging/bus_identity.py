from __future__ import annotations

import platform
import secrets
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.messaging.served_bus import IdentifiedBus, ServedBus, UnidentifiedBus
from agent_comms.nominal.field_codec import NullMember, TaggedRecordMember, TaggedUnion
from agent_comms.storage.locked_store import DocumentChange, LockedStore, RollbackRetained
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore


class BusToken(TaggedUnion, affix="Token"):
    """What a bus's root holds to identify itself, or that it holds nothing yet."""

    @abstractmethod
    def recorded_in(self, record: BusIdentityRecord) -> BusIdentityRecord:
        """The record this token belongs in, asked of the record's own token.

        A root that has minted one answers the record it was read from, unchanged, so the
        update publishes no write; a root that has not mints into a new one.
        """

    @abstractmethod
    def served_by(self, host: str) -> ServedBus:
        """This token as an answer from ``host``."""


@dataclass(frozen=True)
class UnmintedToken(NullMember, BusToken):
    """No Core has served this root since buses began identifying themselves."""

    def recorded_in(self, record: BusIdentityRecord) -> BusIdentityRecord:
        return BusIdentityRecord(MintedToken(secrets.token_hex(16)))

    def served_by(self, host: str) -> ServedBus:
        return UnidentifiedBus()


@dataclass(frozen=True)
class MintedToken(TaggedRecordMember, BusToken):
    """The one token this bus answers for the rest of its life."""

    token: str

    def recorded_in(self, record: BusIdentityRecord) -> BusIdentityRecord:
        return record

    def served_by(self, host: str) -> ServedBus:
        return IdentifiedBus(self.token, host)


@dataclass(frozen=True)
class BusIdentityRecord:
    """A bus root's own file: its token, and nothing a machine could contribute."""

    identity: BusToken = UnmintedToken()


@dataclass(frozen=True, slots=True)
class BusIdentityStore(SingleFileStore, LockedStore[BusIdentityRecord], RollbackRetained,
                       DependentStateTier):
    """One bus's identity file. Two Cores starting on one root at once take the lock, and
    the second reads what the first wrote rather than replacing it."""

    filename = "bus_identity.json"
    record_type = BusIdentityRecord

    def empty(self) -> BusIdentityRecord:
        return BusIdentityRecord()

    def held(self) -> BusToken:
        """What this root already says it is, without writing anything."""
        return self.read().identity

    def minted(self) -> BusToken:
        """This root's token, written on the first ask and answered unchanged after."""
        def mint(record: BusIdentityRecord) -> DocumentChange[BusIdentityRecord, BusToken]:
            changed = record.identity.recorded_in(record)
            return DocumentChange(changed, changed.identity)
        return self.update(mint)

    @staticmethod
    def host() -> str:
        """The label this machine puts on its answers.

        Its hostname, which is what the machine calls itself without asking a network: it
        is a label and not the identity, so it may disagree with the name a reader knows
        the machine by. This laptop's hostname is `ts` while its tailnet calls it `laptop`.
        """
        return platform.node()

    @classmethod
    def serving(cls, root: Path) -> ServedBus:
        """How a Core answers which bus it serves: read, never minted."""
        return cls.at(root).held().served_by(cls.host())

    @classmethod
    def minted_for(cls, root: Path) -> ServedBus:
        """The same answer, from the start of serving, which is where minting belongs."""
        return cls.at(root).minted().served_by(cls.host())
