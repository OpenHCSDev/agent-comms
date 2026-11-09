"""Original message facts read by response-policy implementations."""

from abc import ABC

from agent_comms.messaging.mentions import ThreadMention


class ResponseMessageSource(ABC):
    __slots__ = ()

    seq: int
    mentions: tuple[ThreadMention, ...]
