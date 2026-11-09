"""Each publication kind resolves decisions for its frozen participants."""

from abc import abstractmethod

from agent_comms.messaging.audience_manifest import AudiencedMessage
from agent_comms.messaging.wake import MessageControl, resolve_wake_cohort
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.waking.wake_policy import MessageAudience, RecipientDecision, WakeDecision


class DeliveryPolicy(DeclaredFamily, affix="DeliveryPolicy"):
    @staticmethod
    @abstractmethod
    def resolve(
        message: AudiencedMessage, control: type[MessageControl]
    ) -> tuple[RecipientDecision, ...]: ...


class InitialDeliveryPolicy(DeliveryPolicy):
    @staticmethod
    def resolve(
        message: AudiencedMessage, control: type[MessageControl]
    ) -> tuple[RecipientDecision, ...]:
        return resolve_wake_cohort(message, control=control)


class ResponseDeliveryPolicy(DeliveryPolicy):
    @staticmethod
    def resolve(
        message: AudiencedMessage, control: type[MessageControl]
    ) -> tuple[RecipientDecision, ...]:
        mode = message.role.response_wake_mode()
        return tuple(
            WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, mode)
            for member in message.recipients
        )
