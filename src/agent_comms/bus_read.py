"""Read operations share their declaration; callers supply the read authority."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .comms import Comms
from .owner_connection import (
    AttachedResultRuntimeRequest,
    AttachedRuntimeRequestContext,
    OwnerIdentityChangedError,
    OwnerReading,
)
from .read_basis import NoDisplaySelection, ReadAuthority
from .tool_declaration import ToolDeclaration
from .tool_request import ThreadReadTool
from .tool_result import ToolResult
from .errors import RelationViolationError, UnregisteredThreadError
from .private_path import FileIdentity, ObservedPath
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .registry_store import RegistryCut
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .owner_connection import OwnerConnection


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
        root = FileIdentity.from_stat(comms.root.stat())
        thread = self.require_recipient(cut.snapshot, owner)
        result = await thread.execution.owner_binding(cut.snapshot, thread).read_operation(
            self, comms, owner, cut,
        )
        if not ObservedPath.at(comms.root).same_file(root):
            raise OwnerIdentityChangedError("Wire root changed during the read.")
        self.require_recipient(comms.registry.snapshot(), owner)
        return result

    def require_recipient(self, snapshot: RegistrySnapshot, owner: ThreadIncarnation) -> Thread:
        try:
            thread = snapshot.require(owner.name)
        except UnregisteredThreadError as error:
            raise OwnerIdentityChangedError("The read recipient is no longer registered.") from error
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
