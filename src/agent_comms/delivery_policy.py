"""Each publication kind resolves decisions for its frozen participants."""

from abc import abstractmethod

from .audience_manifest import AudiencedMessage
from .declared_family import DeclaredFamily
from .wake import MessageControl, resolve_wake_cohort
from .wake_policy import MessageAudience, WakeDecision


class DeliveryPolicy(DeclaredFamily, affix="DeliveryPolicy"):
    @staticmethod
    @abstractmethod
    def resolve(message: AudiencedMessage, control: type[MessageControl]): ...


class InitialDeliveryPolicy(DeliveryPolicy):
    @staticmethod
    def resolve(message: AudiencedMessage, control: type[MessageControl]):
        return resolve_wake_cohort(message, control=control)


class ResponseDeliveryPolicy(DeliveryPolicy):
    @staticmethod
    def resolve(message: AudiencedMessage, control: type[MessageControl]):
        mode = message.role.response_wake_mode()
        return tuple(
            WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, mode)
            for member in message.recipients
        )
