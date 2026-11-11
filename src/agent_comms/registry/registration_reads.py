from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from agent_comms.nominal.field_codec import TurnId


class RegistryPathRead(ABC):
    __slots__ = ()
    path: Path


class ActivityRegistration[Owner, NativeOwner, Snapshot, Storage, Route, Lease, Completion](ABC):
    __slots__ = ()
    store: RegistryPathRead
    storage_rule: Storage

    @abstractmethod
    def snapshot(self) -> Snapshot: ...

    @abstractmethod
    def require(self, name: str) -> Owner: ...

    @abstractmethod
    def lease_local_turn(
        self, name: str, turn_id: TurnId, *, routing: Route = ...,
    ) -> tuple[NativeOwner, int]: ...

    @abstractmethod
    def release_turn(self, lease: Lease) -> tuple[bool, Completion]: ...
