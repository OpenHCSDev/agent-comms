"""Bus position and content identity are one reference; neither alone is proof."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MessageReference:
    seq: int
    message_id: str
