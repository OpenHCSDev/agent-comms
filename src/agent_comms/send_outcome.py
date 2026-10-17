"""Send decisions dispatch effects; their awaitables only control forwarding."""
import asyncio
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from collections.abc import Awaitable, Callable

from . import turn_failure as failures


class SendOutcome(ABC):
    @abstractmethod
    def dispatch(self, inputs, session, item, write) -> Awaitable[bool]: ...


class GrantedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        write()
        return self._drain(session)

    async def _drain(self, session):
        await session.stdin.drain()
        return True


class HeldSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        inputs.queue.hold(item)
        return asyncio.sleep(0, result=True)


class RefusedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        item.refuse(session)
        return asyncio.sleep(0, result=True)


class AuthorityChangedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        inputs.uncertain = True
        session.output.final_assistant_stop = False
        session.output.record_failure(failures.AuthorityChanged("Input authority changed before native send."))
        return asyncio.sleep(0, result=False)


SendBoundary = Callable[[str | None, str, str], AbstractContextManager[SendOutcome]]
