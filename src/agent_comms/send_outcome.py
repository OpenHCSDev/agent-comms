"""Send decisions dispatch effects; their awaitables only control forwarding."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING

from . import turn_failure as failures
from .field_codec import InputId

if TYPE_CHECKING:
    from .agent_backend import TurnInbox, TurnInboxItem
    from .backend import NativeSession
    from .turn_inputs import InputForwarding


class SendOutcome(ABC):
    def resume(self, inbox: TurnInbox) -> None:
        pass

    def dispatch(self, inputs: InputForwarding, session: NativeSession,
                 item: TurnInboxItem, write: Callable[[], None]) -> Awaitable[bool]:
        completion = self.apply(inputs, session, item, write)
        # Transfer ownership before the first await: cancellation during drain
        # must not put an already-written input back in the inbox.
        inputs.release_current()
        return completion

    @abstractmethod
    def apply(self, inputs: InputForwarding, session: NativeSession,
              item: TurnInboxItem, write: Callable[[], None]) -> Awaitable[bool]: ...


class GrantedSend(SendOutcome):
    def resume(self, inbox: TurnInbox) -> None:
        inbox.ready.set()

    def apply(self, inputs: InputForwarding, session: NativeSession,
              item: TurnInboxItem, write: Callable[[], None]) -> Awaitable[bool]:
        write()
        return self._drain(session)

    async def _drain(self, session: NativeSession) -> bool:
        await session.native.drain()
        return True


class HeldSend(SendOutcome):
    def apply(self, inputs: InputForwarding, session: NativeSession,
              item: TurnInboxItem, write: Callable[[], None]) -> Awaitable[bool]:
        inputs.queue.hold(item)
        return asyncio.sleep(0, result=True)


class DeferredSend(SendOutcome):
    """Leave an unwritten input with its live acceptance, outside this native turn."""

    def apply(self, inputs: InputForwarding, session: NativeSession,
              item: TurnInboxItem, write: Callable[[], None]) -> Awaitable[bool]:
        return asyncio.sleep(0, result=True)


class RefusedSend(SendOutcome):
    def apply(self, inputs: InputForwarding, session: NativeSession,
              item: TurnInboxItem, write: Callable[[], None]) -> Awaitable[bool]:
        item.refuse(inputs, session)
        return asyncio.sleep(0, result=True)


class AuthorityChangedSend(SendOutcome):
    def apply(self, inputs: InputForwarding, session: NativeSession,
              item: TurnInboxItem, write: Callable[[], None]) -> Awaitable[bool]:
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
