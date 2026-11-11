"""A captured channel batch is an input proof, not an orchestration flag."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from agent_comms.execution.input_key import InputKey
from agent_comms.execution.original_inputs import OriginalInputs
from agent_comms.input_attempt import StoredInput
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.messages import Message
from agent_comms.registry.thread_creation import ThreadCreationSource


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
        owner: ThreadCreationSource,
        document: OriginalInputs,
    ) -> InputBatch:
        originals = document.originals(keys)
        single = SingleInputBatch(originals)
        # Sequence identity proves distinct channel originals in authorized order.
        channels = {
            origin.seq: origin
            for origin in origins
            if CommandTarget.named(origin.target).accepts_batch(origin.seq)
        }
        if len(channels) < 2 or tuple(channels.values()) != origins:
            return single
        expected_keys = tuple(origin.bus_key(owner) for origin in origins)
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
