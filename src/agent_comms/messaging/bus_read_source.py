"""The existing bus snapshot and unread-count operations presentation consumes."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager

from agent_comms.messaging.bus import BusSnapshot
from agent_comms.messaging.published_bus import BusResources
from agent_comms.messaging.read_basis import DisplayViewer
from agent_comms.messaging.read_selection import CapturedSelection


class BusReadSource(BusResources, ABC):
    __slots__ = ()

    @abstractmethod
    def snapshot(self, viewer: DisplayViewer) -> AbstractContextManager[tuple[CapturedSelection, BusSnapshot]]:
        ...

    @abstractmethod
    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        ...
