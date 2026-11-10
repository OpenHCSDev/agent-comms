"""The original publication's frozen recipients justify retained recipient reads."""

from abc import ABC

from agent_comms.messaging.frozen_recipient import FrozenRecipient


class DeliveryRecipients(ABC):
    __slots__ = ()
    recipients: tuple[FrozenRecipient, ...]
