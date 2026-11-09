"""Native input acquisition contracts implemented by the original live owners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from agent_comms.input_lookup import InputLookup
from agent_comms.nominal.field_codec import RootId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import ClaimedIdentity
from agent_comms.registry.user_thread_lookup import UserThreadLookup
from agent_comms.task_owner import TaskOwner
from agent_comms.task_registry import TaskRegistry


class InputRootAuthority(ABC):
    __slots__ = ()

    @property
    @abstractmethod
    def namespace(self) -> RootId:
        """Require the original native namespace; a public registry has none."""

    @abstractmethod
    def require_root(self, root_id: RootId, error: Exception) -> None: ...


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


class InputTextContext[Attempt](ABC):
    __slots__ = ()
    root: Path
    input_dispositions: Callable[[Path], InputLookup[Attempt]]
