"""The original session resources and owner lifetime consumed during retirement."""

import asyncio
from abc import abstractmethod
from contextlib import AbstractAsyncContextManager

from agent_comms.config_options import ConfigOptions
from agent_comms.execution.session_reads import OwnedObservation, SessionIdentity
from agent_comms.execution.session_tasks import TurnTask
from agent_comms.registry.thread_identity import OwnerIdentity


class SessionTaskOwner(SessionIdentity):
    """An existing session exposes and retires its own tasks."""

    @abstractmethod
    def producers(self) -> tuple[asyncio.Task[object], ...]: ...

    @abstractmethod
    def retire_producers(self) -> None: ...

    @abstractmethod
    def retire_turn_task(self) -> TurnTask: ...


class OwnedSessionLifetime(OwnedObservation, SessionTaskOwner):
    """An original execution owner admits work and joins it before release."""

    configuration: ConfigOptions
    owner: OwnerIdentity
    turn_lock: asyncio.Lock
    retirement_lock: asyncio.Lock
    retired: asyncio.Future[None]

    @abstractmethod
    def own_turn_task(self) -> AbstractAsyncContextManager[None]: ...

    @abstractmethod
    def request_stop(self) -> None: ...

    @abstractmethod
    async def join_observations(self) -> None: ...
