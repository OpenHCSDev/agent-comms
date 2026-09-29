"""A captured channel batch is an input proof, not an orchestration flag."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .channel_targets import is_channel_target

if TYPE_CHECKING:
    from .input_disposition import InputDispositions
    from .messages import Message
    from .threads import Thread


class InputBatch(ABC):
    @property
    @abstractmethod
    def admits_multiple(self) -> bool: ...

    @staticmethod
    def capture(
        origins: tuple[Message, ...],
        keys: tuple[str, ...],
        prompt: str,
        owner: Thread,
        dispositions: InputDispositions,
    ) -> InputBatch:
        # Sequence identity makes duplicates collapse. Equality then proves every
        # origin was a distinct positive channel message, in the admitted order.
        channels = {
            origin.seq: origin
            for origin in origins
            if origin.seq > 0 and is_channel_target(origin.target)
        }
        if len(channels) < 2 or tuple(channels.values()) != origins:
            return SingleInputBatch()
        expected_keys = tuple(dispositions.bus_key(origin, owner) for origin in origins)
        texts = dispositions.read().source_texts(keys)
        if texts is None:
            return SingleInputBatch()
        admitted = ChannelInputBatch(expected_keys, "\n\n".join(texts))
        # Durable text, including admission-time resolved names, is authoritative.
        return admitted if admitted == ChannelInputBatch(keys, prompt) else SingleInputBatch()


class SingleInputBatch(InputBatch):
    @property
    def admits_multiple(self) -> bool:
        return False


@dataclass(frozen=True)
class ChannelInputBatch(InputBatch):
    keys: tuple[str, ...]
    prompt: str

    @property
    def admits_multiple(self) -> bool:
        return True
