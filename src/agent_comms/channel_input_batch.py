"""A captured channel batch is an input proof, not an orchestration flag."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .input_disposition import InputDispositions

from .input_key import InputKey

from .channel_targets import CommandTarget
from .input_attempt import StoredInput

if TYPE_CHECKING:
    from .input_disposition import InputDocument
    from .messages import Message
    from .threads import Thread


@dataclass(frozen=True)
class InputBatch(ABC):
    originals: tuple[StoredInput, ...]

    @property
    def keys(self) -> tuple[InputKey, ...]:
        return tuple(row.key for row in self.originals)

    @property
    def prompt(self) -> str:
        return "\n\n".join(row.source_text for row in self.originals)

    @property
    def native_reply_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            target for row in self.originals if (target := row.origin.reply_target)
        ))

    @property
    @abstractmethod
    def allows_multiple(self) -> bool: ...

    @staticmethod
    def capture(
        origins: tuple[Message, ...],
        keys: tuple[InputKey, ...],
        prompt: str,
        owner: Thread,
        document: InputDocument,
    ) -> InputBatch:
        originals = document.originals(keys)
        single = SingleInputBatch(originals)
        # Sequence identity proves distinct channel originals in authorized order.
        channels = {
            origin.seq: origin
            for origin in origins
            if origin.seq > 0 and CommandTarget.named(origin.target).batchable
        }
        if len(channels) < 2 or tuple(channels.values()) != origins:
            return single
        expected_keys = tuple(InputDispositions.bus_key(origin, owner) for origin in origins)
        authorized = ChannelInputBatch(originals)
        return authorized if authorized.keys == expected_keys and authorized.prompt == prompt else single


class SingleInputBatch(InputBatch):
    @property
    def allows_multiple(self) -> bool:
        return False


@dataclass(frozen=True)
class ChannelInputBatch(InputBatch):
    @property
    def allows_multiple(self) -> bool:
        return True
