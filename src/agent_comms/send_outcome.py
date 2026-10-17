"""Native send decisions own their effects; none is a truth-valued grant."""
from contextlib import AbstractContextManager
from collections.abc import Callable

from . import turn_failure as failures


class SendOutcome:
    def dispatch(self, inputs, session, item, write):
        raise NotImplementedError

    async def complete(self, session):
        return True


class GrantedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        write()

    async def complete(self, session):
        await session.stdin.drain()
        return True


class HeldSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        inputs.queue.hold(item)


class RefusedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        item.refuse(session)


class AuthorityChangedSend(SendOutcome):
    def dispatch(self, inputs, session, item, write):
        inputs.uncertain = True
        session.output.final_assistant_stop = False
        session.output.record_failure(failures.AuthorityChanged("Input authority changed before native send."))

    async def complete(self, session):
        return False


SendBoundary = Callable[[str | None, str, str], AbstractContextManager[SendOutcome]]
