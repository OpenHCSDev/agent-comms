"""Read operations share their declaration; callers supply the read authority."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import partial

from .comms import Comms
from .coordination_errors import StaleFence
from .coordinator import Coordination
from .errors import RelationViolationError, UnregisteredThreadError
from .owner_connection import (
    OwnerConnection,
    OwnerIdentityChangedError,
    OwnerReading,
    OwnerReplyConnection,
)
from .private_path import FileRevision, ObservedPath
from .runtime_server import AttachedResultRuntimeRequest, AttachedRuntimeRequestContext
from .read_basis import NoDisplaySelection, ReadAuthority
from .registry_document import RegistrySnapshot
from .registry_store import RegistryCut
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .tool_declaration import ToolDeclaration
from .tool_request import ThreadReadTool
from .tool_result import ToolResult


class BusRead[Result](ToolDeclaration, ABC):
    """One operation owns its arguments and its read; the boundary supplies authority."""

    @abstractmethod
    def apply(self, comms: Comms, caller: ReadAuthority) -> Result: ...


class UiRead[Result](BusRead[Result], ABC):
    def read(self, comms: Comms) -> Result:
        return self.apply(comms, NoDisplaySelection())


class OwnerScopedRead[Result: ToolResult = ToolResult](BusRead[Result], ThreadReadTool, ABC):
    """The captured registration selects live evidence or the declared unavailable answer."""

    def apply(self, comms: Comms, caller: ReadAuthority) -> Result:
        cut = comms.registry.store.snapshot_with_authority()
        owner = cut.snapshot.require(caller.subject(self.thread)).incarnation
        return asyncio.run(self.answer(comms, owner, cut))

    async def answer(self, comms: Comms, owner: ThreadIncarnation, cut: RegistryCut) -> Result:
        root = FileRevision.capture(comms.root).require(
            OwnerIdentityChangedError("Wire root disappeared before the read.")
        ).identity
        thread = self.require_recipient(cut.snapshot, owner)
        binding = thread.execution.owner_binding(cut.snapshot, thread)
        read = binding.select_read(
            partial(self.unavailable_answer, comms, owner, cut),
            partial(self.live_answer, comms, owner),
        )
        result = await read()
        current = await Coordination.run_worker(comms.registry.snapshot)
        if not ObservedPath.at(comms.root).same_file(root):
            raise OwnerIdentityChangedError("Wire root changed during the read.")
        self.require_recipient(current, owner)
        return result

    async def unavailable_answer(
        self, comms: Comms, owner: ThreadIncarnation, cut: RegistryCut,
    ) -> Result:
        """Keep blocking retained reads inside the captured registry cut's worker."""
        def retained() -> Result:
            with cut.current():
                result = self.unavailable(comms, owner)
                if not cut.matches_current():
                    raise StaleFence("Registry changed during the retained read")
                return result

        return await Coordination.run_worker(retained)

    async def live_answer(self, comms: Comms, owner: ThreadIncarnation) -> Result:
        request = OwnerReadRuntimeRequest(thread=owner.name, operation=self)
        return self.decode_reply(await OwnerReplyConnection.answer(comms, owner, request))

    def require_recipient(self, snapshot: RegistrySnapshot, owner: ThreadIncarnation) -> Thread:
        try:
            thread = snapshot.require(owner.name)
        except UnregisteredThreadError as error:
            raise OwnerIdentityChangedError(
                "The read recipient is no longer registered."
            ) from error
        if (thread.name, thread.incarnation) != (owner.name, owner):
            raise OwnerIdentityChangedError("The read recipient name or incarnation changed.")
        return thread

    @abstractmethod
    def unavailable(self, comms: Comms, owner: ThreadIncarnation) -> Result: ...

    @abstractmethod
    async def owned(self, ctx: AttachedRuntimeRequestContext) -> Result: ...

    @abstractmethod
    def decode_reply(self, raw: object) -> Result: ...


class OwnerLiveRead[Result: ToolResult = ToolResult](OwnerScopedRead[Result], ABC):
    """This subfamily has no answer without the selected live native owner."""

    def unavailable(self, comms: Comms, owner: ThreadIncarnation) -> Result:
        raise RelationViolationError("The read requires the live native owner.")


@dataclass(frozen=True, kw_only=True)
class OwnerReadRuntimeRequest(OwnerReading[object], AttachedResultRuntimeRequest):
    operation: OwnerScopedRead

    async def read(self, owner: OwnerConnection) -> object:
        return await owner.request(self.declared_name, operation=self.operation)

    async def result(self, ctx: AttachedRuntimeRequestContext) -> ToolResult:
        return await self.operation.owned(ctx)
