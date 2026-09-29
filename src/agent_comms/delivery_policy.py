"""Committed delivery kinds own their wake semantics at the wire boundary."""

from abc import abstractmethod
from typing import ClassVar

from .coordination_tables.assignments import MessageAudience
from .declared_family import DeclaredFamily
from .wake import WakeDecision, resolve_wake_cohort
from .wake_policy import BoundedTriageWake, PassiveWake


class DeliveryPolicy(DeclaredFamily, affix="DeliveryPolicy"):
    fields: ClassVar[frozenset[str]]

    @classmethod
    def from_private(cls, private):
        shape = frozenset(private)
        matches = [member for member in cls.members_with(cls) if member.fields == shape]
        if len(matches) != 1:
            raise ValueError("Unsupported committed delivery sideband.")
        return matches[0]()

    @abstractmethod
    def resolve(self, message, audience, control): ...


class InitialDeliveryPolicy(DeliveryPolicy):
    fields = frozenset({"version", "initial"})

    def resolve(self, message, audience, control):
        return resolve_wake_cohort(message, frozen_audience=audience, control=control)


class ResponseDeliveryPolicy(DeliveryPolicy):
    fields = frozenset({"version", "initial", "response"})

    def resolve(self, message, audience, control):
        # Every frozen conversation member can consider the information. Even a
        # DM or explicit mention in an informative reply does not require ACK.
        mode = PassiveWake() if message.notice else BoundedTriageWake()
        return tuple(
            WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, mode)
            for member in audience.recipients
        )
