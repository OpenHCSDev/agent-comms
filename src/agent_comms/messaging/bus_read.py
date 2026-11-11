from __future__ import annotations

import asyncio
from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import dataclass
from functools import partial
from typing import cast

from agent_comms.acp_extension import TranscriptSnapshotUpdate
from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import StaleFence
from agent_comms.errors import UnregisteredThreadError
from agent_comms.messaging.read_basis import NoDisplaySelection, ReadAuthority
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_store import RegistryCut
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.private_path import FileRevision, ObservedPath
from agent_comms.transcript.transcripts import TranscriptRead
from agent_comms.transport.owner_connection import (
    OwnerConnection,
    OwnerIdentityChangedError,
    OwnerReading,
    OwnerReplyConnection,
)
from agent_comms.transport.runtime_request import AttachedResultRuntimeRequest
from agent_comms.transport.tool_declaration import ToolDeclaration
from agent_comms.transport.tool_request import ThreadReadTool
from agent_comms.transport.tool_result import ToolResult


class BusRead[Result](ToolDeclaration, ABC):
    """One operation owns its arguments and its read; the boundary supplies authority."""

    @abstractmethod
    def apply(self, comms: Comms, caller: ReadAuthority) -> Result: ...


class UiRead[Result](BusRead[Result], ABC):
    def read(self, comms: Comms) -> Result:
        return self.apply(comms, NoDisplaySelection())


class CapturesTranscriptPage(UiRead, ABC):
    """A read whose answer is one captured transcript page: the page is the serving work,
    and the snapshot the wire carries is derived from it."""

    @abstractmethod
    def capture_as(self, comms: Comms, reader: ReadAuthority) -> TranscriptRead: ...

    def apply(self, comms: Comms, caller: ReadAuthority) -> TranscriptSnapshotUpdate:
        return TranscriptSnapshotUpdate.from_read(self.capture_as(comms, caller))


class OwnerScopedRead[Result: ToolResult = ToolResult](
    BusRead[Result], ThreadReadTool[Result], ABC,
):
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
        binding = thread.owner_binding(cut.snapshot)
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

    def decode_reply(self, raw: object) -> Result:
        """The owner's reply, decoded into the result the declaration binds."""
        return cast(Result, FieldCodec.decode(self.result_type(), raw))


@dataclass(frozen=True, kw_only=True)
class OwnerReadRuntimeRequest(OwnerReading[object], AttachedResultRuntimeRequest):
    operation: OwnerScopedRead

    async def read(self, owner: OwnerConnection) -> object:
        return await owner.request(self.declared_name, operation=self.operation)
