"""The actual notification lends only the facts used by progress/activity callbacks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from agent_comms.messaging.frozen_recipient import FrozenRecipient


class AssignmentNotice[State](ABC):
    __slots__ = ()
    recipient_identity: FrozenRecipient
    state: State

    @property
    @abstractmethod
    def label(self) -> str: ...

    @staticmethod
    @abstractmethod
    def timed_state(label: str, timestamp_ms: int | None) -> str: ...

    @abstractmethod
    def with_state(self, state: State) -> Self: ...
