"""A captured channel batch is an input proof, not an orchestration flag."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .channel_targets import is_channel_target
from .input_attempt import StoredInput

if TYPE_CHECKING:
    from .input_disposition import InputDocument
    from .messages import Message
    from .threads import Thread


@dataclass(frozen=True)
class InputBatch(ABC):
    originals: tuple[StoredInput, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(row.key for row in self.originals)

    @property
    def prompt(self) -> str:
        return "\n\n".join(row.source_text for row in self.originals)

    @property
    @abstractmethod
    def admits_multiple(self) -> bool: ...

    @staticmethod
    def capture(
        origins: tuple[Message, ...],
        keys: tuple[str, ...],
        prompt: str,
        owner: Thread,
        document: InputDocument,
    ) -> InputBatch:
        from .input_disposition import InputDispositions

        originals = document.originals(keys)
        single = SingleInputBatch(originals)
        # Sequence identity proves distinct channel originals in admitted order.
        channels = {
            origin.seq: origin
            for origin in origins
            if origin.seq > 0 and is_channel_target(origin.target)
        }
        if len(channels) < 2 or tuple(channels.values()) != origins:
            return single
        expected_keys = tuple(InputDispositions.bus_key(origin, owner) for origin in origins)
        admitted = ChannelInputBatch(originals)
        return admitted if admitted.keys == expected_keys and admitted.prompt == prompt else single


class SingleInputBatch(InputBatch):
    @property
    def admits_multiple(self) -> bool:
        return False


@dataclass(frozen=True)
class ChannelInputBatch(InputBatch):
    @property
    def admits_multiple(self) -> bool:
        return True
