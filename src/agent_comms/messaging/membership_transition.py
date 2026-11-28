"""Channel membership transitions, declared below Message so channels never import messages."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from agent_comms.messaging.bus_activity_index import SentActivity
from agent_comms.messaging.projection_source import ProducedRow, ProjectionSource
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import NullMember


class RoleActivitySource(ProjectionSource):
    """A message's role and membership write its sender's activity and its awaited answers."""

    __slots__ = ()

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from agent_comms.messaging.bus_tables import AwaitingAnswerSource
        return (SentActivity, AwaitingAnswerSource)


class MembershipTransition(DeclaredFamily, RoleActivitySource, affix="Membership"):
    @classmethod
    def conversation_role[Role](
        cls,
        membership_role: Callable[[type[MembershipTransition]], Role],
        ordinary_role: Callable[[], Role],
    ) -> Role:
        del ordinary_role
        return membership_role(cls)

    @classmethod
    def between(cls, previous: bool, current: bool) -> tuple[type[MembershipTransition], ...]:
        return tuple(member for member in cls.members_with(cls) if member.applies(previous, current))

    @staticmethod
    @abstractmethod
    def applies(previous: bool, current: bool) -> bool: ...


class JoinedMembership(MembershipTransition):
    @staticmethod
    def applies(previous: bool, current: bool) -> bool:
        return not previous and current


class LeftMembership(MembershipTransition):
    @staticmethod
    def applies(previous: bool, current: bool) -> bool:
        return previous and not current


@dataclass(frozen=True, slots=True)
class NoMembershipChange(NullMember, RoleActivitySource):
    def conversation_role[Role](
        self,
        membership_role: Callable[[type[MembershipTransition]], Role],
        ordinary_role: Callable[[], Role],
    ) -> Role:
        del membership_role
        return ordinary_role()
