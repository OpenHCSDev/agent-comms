"""The actual retained session owns its child, borrowing lock and retirement."""
from abc import ABC, abstractmethod
from asyncio import Lock


class NativeSessionRetention[Child, State, Identity](ABC):
    __slots__ = ()
    lock: Lock
    child_state: State

    @abstractmethod
    async def open(self, launch, *, reuse, startup, watchdog) -> Child: ...

    @abstractmethod
    async def context_for_input(self, thread, context): ...

    @abstractmethod
    def retain(self, child: Child, identity: Identity) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def close_idle(self) -> None: ...
