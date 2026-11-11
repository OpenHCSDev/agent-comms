"""Native input acquisition contracts implemented by the original live owners."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent_comms.execution.input_root_authority import InputRootAuthority
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry
from agent_comms.registry.thread_identity import ClaimedIdentity
from agent_comms.registry.user_thread_lookup import UserThreadLookup


class InputRegistry[Owner: TaskOwner](TaskRegistry[Owner]):
    __slots__ = ()

    @abstractmethod
    def require_active(self, name: str) -> Owner: ...

    @abstractmethod
    def claimed_identity(self, name: str) -> ClaimedIdentity: ...

    @abstractmethod
    def user_thread(self) -> UserThreadLookup[Owner]: ...


class InputAuthorityCut[Owner: TaskOwner](ABC):
    __slots__ = ()
    snapshot: InputRegistry[Owner]
    privacy: InputRootAuthority


class InputRegistryStore[Owner: TaskOwner](ABC):
    __slots__ = ()

    @abstractmethod
    def snapshot_with_authority(self) -> InputAuthorityCut[Owner]: ...

    @abstractmethod
    def privacy(self) -> InputRootAuthority: ...


class InputRegistration[Owner: TaskOwner](ABC):
    __slots__ = ()
    store: InputRegistryStore[Owner]


class InputUserIdentity[Owner: TaskOwner](ABC):
    __slots__ = ()

    @abstractmethod
    def user_identity(self, worktree: Checkout) -> Owner: ...


class InputIngressContext[Owner: TaskOwner](ABC):
    __slots__ = ()
    registry: InputRegistration[Owner]
    messaging: InputUserIdentity[Owner]
