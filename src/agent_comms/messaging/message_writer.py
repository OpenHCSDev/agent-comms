"""The original bus append operations used by initial publication."""

from __future__ import annotations

from abc import ABC, abstractmethod

import agent_comms.bus_record_builder as bus_record_builder
from agent_comms.messaging.sending import messages


class MessageWriter(ABC):
    __slots__ = ()

    @abstractmethod
    def append(self, build: bus_record_builder.RecordBuilder) -> messages.BusRecord: ...

    @abstractmethod
    def append_original(self, build: bus_record_builder.RecordBuilder) -> messages.BusRecord: ...
