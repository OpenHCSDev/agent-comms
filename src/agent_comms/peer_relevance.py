"""How much one thread matters to another: a family whose members carry capabilities.

The relations in ``relationships.py`` declare which member they grant, and
``ThreadRelationships`` answers which one holds between two threads. Consumers
ask a member what it ``grants``; they never name or compare members.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence

from .declared_family import DeclaredFamily


class RelevanceCapability(ABC):
    """Something a peer's relevance guarantees to the thread it is relevant to."""


class Visible(RelevanceCapability):
    """The peer appears in the peer list while slots remain."""


class AlwaysListed(RelevanceCapability):
    """The peer is in the peer list whatever its name and however many threads exist."""


class Prioritized(RelevanceCapability):
    """The peer's messages surface first in awareness."""


class Informed(RelevanceCapability):
    """The thread is told when the peer's goal changes."""


class DirectWake(RelevanceCapability):
    """The peer's direct messages wake the thread without a triage call."""


class PeerRelevance(DeclaredFamily, affix="Relevance"):
    """How much one thread matters to another.

    A member guarantees everything the members it derives from guarantee, so
    precedence is the class hierarchy itself. Relevance decides what a thread
    sees and hears; it grants no permission.
    """

    @classmethod
    def grants(cls, capability: type[RelevanceCapability]) -> bool:
        return issubclass(cls, capability)

    @classmethod
    def most_derived(cls, granted: Sequence[type[PeerRelevance]]) -> type[PeerRelevance]:
        for member in granted:
            if all(issubclass(member, other) for other in granted):
                return member
        raise TypeError(f"Granted relevance has no single most derived member: {granted!r}")


class StrangerRelevance(PeerRelevance):
    """Every registered peer, with nothing else known about it."""


class ChannelmateRelevance(StrangerRelevance, Visible):
    """A peer that shares a tag channel."""


class GoalContactRelevance(ChannelmateRelevance, AlwaysListed, Prioritized):
    """A peer bound to an active goal by a mention."""


class CollaboratorRelevance(GoalContactRelevance, Informed, DirectWake):
    """A peer in an explicit collaboration."""
