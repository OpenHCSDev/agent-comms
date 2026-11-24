"""Wire selection for visible team-board pages, shared by reader and client view."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from agent_comms.nominal.field_codec import NullMember, TaggedRecordMember, TaggedUnion
from agent_comms.registry.thread_identity import ThreadIncarnation


class TeamBoardSelection(TaggedUnion, affix="TeamBoards"):
    """The recipients requested by visible board pages, shared by reader and view."""

    @classmethod
    def named(cls, name: str) -> TeamBoardSelection:
        """An empty user filter requests the whole board."""
        return NamedTeamBoards(frozenset((name,))) if name else AllTeamBoards()

    @abstractmethod
    def includes(self, recipient: ThreadIncarnation) -> bool: ...

    @abstractmethod
    def combined(self, other: TeamBoardSelection) -> TeamBoardSelection: ...

    @abstractmethod
    def with_names(self, names: frozenset[str]) -> TeamBoardSelection: ...


@dataclass(frozen=True)
class AllTeamBoards(NullMember, TeamBoardSelection):
    def includes(self, recipient: ThreadIncarnation) -> bool:
        return True

    def combined(self, other: TeamBoardSelection) -> TeamBoardSelection:
        return self

    def with_names(self, names: frozenset[str]) -> TeamBoardSelection:
        return self


@dataclass(frozen=True)
class NamedTeamBoards(TaggedRecordMember, TeamBoardSelection):
    names: frozenset[str]

    def includes(self, recipient: ThreadIncarnation) -> bool:
        return recipient.name in self.names

    def combined(self, other: TeamBoardSelection) -> TeamBoardSelection:
        return other.with_names(self.names)

    def with_names(self, names: frozenset[str]) -> TeamBoardSelection:
        return type(self)(self.names | names)
