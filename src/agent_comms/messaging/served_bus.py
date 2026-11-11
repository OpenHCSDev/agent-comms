"""Which bus answered, said so that two of them can be told apart.

A bus is a directory, and this fleet runs every machine as the same user with its bus at
the same path, so `comms_served_root` answered the same bytes on all of them. On
2026-11-07 this session's own connector was refused at its MCP handshake, a second
connector answered for another machine's bus for an hour, and the thread it read as its
own was that machine's thread of the same name: it reported those tags to Tristan as its
own. Nothing in any answer disagreed. `DisplayReferences.at` exists to refuse a foreign
catalog and compares those same path strings, so it could not fire.

A bus therefore answers an identity minted once at its root, which decides sameness, and
a path and a host name, which are labels. Neither label can be the identity: a path says
nothing about whose filesystem it is on, and this machine's hostname is `ts` while its
tailnet and the bus picker both call it `laptop`.

The wire value lives here, apart from the minting in `bus_identity.py`, because a client
decodes it without taking on a bus's storage: `remote_core` and Toad's phone client read
this family, and only a serving Core writes one.

`RecordedBusIdentity` stays what it is: a bus file's device and inode, which answers "was
this file replaced under the page I painted" on one machine and nothing at all across two.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from agent_comms.nominal.field_codec import NullMember, TaggedRecordMember, TaggedUnion


class ServedBus(TaggedUnion, affix="Bus"):
    """Which bus a Core serves, as that Core says it."""

    @abstractmethod
    def same_bus(self, other: ServedBus) -> bool:
        """Whether both answers came from one bus.

        Asked of one answer about another, so a reader holding two connectors can tell
        whether it is holding one bus twice.
        """

    @abstractmethod
    def holds(self, token: str) -> bool:
        """Whether this bus is the one that minted ``token``.

        How `same_bus` asks the other answer without naming its members, and the reason
        an unidentified answer cannot be talked into equality from either side.
        """

    @abstractmethod
    def announced(self, root: str) -> str:
        """What the MCP handshake tells a client about the bus it has reached.

        The handshake says it because a client that learns this from a tool call learns it
        after reading something, and reading the wrong bus is the whole failure.
        """


@dataclass(frozen=True)
class UnidentifiedBus(NullMember, ServedBus):
    """The answer says only where the bus's files are.

    Two Cores answer this: one older than this family, which never had the field, and one
    serving a root that has never minted a token. Both are answers a reader must not
    resolve, so this claims sameness with nothing, itself included.
    """

    def same_bus(self, other: ServedBus) -> bool:
        return False

    def holds(self, token: str) -> bool:
        return False

    def announced(self, root: str) -> str:
        return (f"This connector serves the bus at {root}, and does not say which machine "
                "that path is on. Other machines in this fleet keep a bus at the same path "
                "and run threads under the same names, so a thread you find here may not be "
                "the one you mean.")


@dataclass(frozen=True)
class IdentifiedBus(TaggedRecordMember, ServedBus):
    """The token minted at the bus's root, with the machine serving it as a label.

    The token is the whole of the identity: a bus copied to another machine keeps it, and a
    machine renamed keeps it, which is why `same_bus` reads nothing else.
    """

    token: str
    host: str

    def same_bus(self, other: ServedBus) -> bool:
        return other.holds(self.token)

    def holds(self, token: str) -> bool:
        return token == self.token

    def announced(self, root: str) -> str:
        return (f"This connector serves the bus at {root} on {self.host}, identified as "
                f"{self.token}. Other machines in this fleet keep a bus at that same path "
                "and run threads under the same names, so before you read a thread here as "
                "your own, check that this is the bus you mean.")
