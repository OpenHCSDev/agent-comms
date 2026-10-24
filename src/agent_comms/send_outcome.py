"""Send decisions dispatch effects; their awaitables only control forwarding."""
import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterator
from contextlib import AbstractContextManager, contextmanager

from . import turn_failure as failures
from .field_codec import InputId


class SendOutcome(ABC):
    def resume(self, inbox):
        pass

    @abstractmethod
    def dispatch(self, inputs, session, item, write) -> Awaitable[bool]: ...


class GrantedSend(SendOutcome):
    def resume(self, inbox):
        inbox.ready.set()

    def dispatch(self, inputs, session, item, write):
        write()
        return self._drain(session)

    async def _drain(self, session):
        await session.native.drain()
        return True


class HeldSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        inputs.queue.hold(item)
        return asyncio.sleep(0, result=True)


class DeferredSend(SendOutcome):
    """Leave an unwritten input with its live acceptance, outside this native turn."""

    def dispatch(self, inputs, session, item, write):
        return asyncio.sleep(0, result=True)


class RefusedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        item.refuse(inputs, session)
        return asyncio.sleep(0, result=True)


class AuthorityChangedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        inputs.uncertain = True
        session.output.final_assistant_stop = False
        session.output.record_failure(failures.AuthorityChanged("Input authority changed before native send."))
        return asyncio.sleep(0, result=False)


class SendBoundary(ABC):
    """Permission to write original, following and interrupt commands."""

    @abstractmethod
    def send_original(
        self, native_id: InputId, text: str
    ) -> AbstractContextManager[SendOutcome]: ...

    @abstractmethod
    def send_following(
        self, public_id: InputId, native_id: InputId, text: str,
    ) -> AbstractContextManager[SendOutcome]: ...

    @abstractmethod
    def send_interrupt(
        self, public_id: InputId, native_id: InputId, text: str,
    ) -> AbstractContextManager[SendOutcome]: ...


class NoSendBoundary(SendBoundary):
    """A direct native session has no ordinary-owner permission to check."""

    @contextmanager
    def _granted(self) -> Iterator[SendOutcome]:
        yield GrantedSend()

    def send_original(self, native_id: InputId, text: str) -> AbstractContextManager[SendOutcome]:
        return self._granted()

    def send_following(
        self, public_id: InputId, native_id: InputId, text: str,
    ) -> AbstractContextManager[SendOutcome]:
        return self._granted()

    def send_interrupt(
        self, public_id: InputId, native_id: InputId, text: str,
    ) -> AbstractContextManager[SendOutcome]:
        return self._granted()


NO_SEND_BOUNDARY = NoSendBoundary()
