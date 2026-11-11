"""Command declarations bind original thread/channel targets in their own context."""

from abc import ABC, abstractmethod
from typing import Self


class TargetCommandBindings[Context, Owner, Presence, Around, Project, Snapshot, Catalog, Resolved](ABC):
    __slots__ = ()

    @classmethod
    @abstractmethod
    def thread_bindings(
        cls, comms: Context, thread: Owner, status: Presence, channel: Around,
        *, catalog: Catalog,
    ) -> tuple[Self, ...]: ...

    @classmethod
    @abstractmethod
    def channel_bindings(
        cls, comms: Context, resolved: Resolved,
        *, project: Project, snapshot: Snapshot, catalog: Catalog,
    ) -> tuple[Self, ...]: ...
