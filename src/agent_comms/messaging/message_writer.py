"""The original bus append operations used by initial publication."""

from __future__ import annotations

from abc import ABC, abstractmethod

import agent_comms.messaging.bus_record_builder as bus_record_builder
import agent_comms.messaging.messages as messages


class MessageWriter(ABC):
    __slots__ = ()

    @abstractmethod
    def append(self, build: bus_record_builder.RecordBuilder) -> messages.BusRecord: ...

    @abstractmethod
    def append_original(self, build: bus_record_builder.RecordBuilder) -> messages.BusRecord: ...
