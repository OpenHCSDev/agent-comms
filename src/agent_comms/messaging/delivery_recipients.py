"""The original publication's frozen recipients justify retained recipient reads."""

from abc import ABC

from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.nominal.field_codec import ThreadLookup


class DeliveryRecipients(ABC):
    __slots__ = ()
    sender_lookup: ThreadLookup
    recipients: tuple[FrozenRecipient, ...]
