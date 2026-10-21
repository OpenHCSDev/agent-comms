"""Typed live ACP text and unconditional saved-history snapshot publication."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from functools import cached_property, partial
from typing import Any, TYPE_CHECKING

from acp.schema import AgentMessageChunk, TextContentBlock

from .acp_extension import (
    TurnChangedUpdate,
    TranscriptSnapshotUpdate,
    encode_updates,
)
from .comms import Comms
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .turn_lease import TurnState
from .ui_model.observation import ObservationProcess, ReadTranscript, SubscribeTranscript

if TYPE_CHECKING:
    from .runtime import RuntimeServer, SocketClient


@dataclass(frozen=True, kw_only=True)
class TranscriptUpdate(DeclaredFamily, affix="TranscriptUpdate"):
    @abstractmethod
    async def publish(self, session_id: str, client: Any) -> None: ...


@dataclass(frozen=True, kw_only=True)
class TurnTranscriptUpdate(TranscriptUpdate):
    state: TurnState

    async def publish(self, session_id: str, client: Any) -> None:
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(TurnChangedUpdate(self.state)),
            ),
        )


class TranscriptReplay:
    def __init__(self, comms: Comms, runtime: RuntimeServer):
        self.comms, self.runtime = comms, runtime

    @cached_property
    def reads(self):
        # Replay and subscription share this process and its request/wait lifetime.
        return self.runtime._resources.enter_context(ObservationProcess.awaited(self.comms.route))

    async def replay(self, session_id: str, client: RuntimeServer | SocketClient) -> None:
        snapshot = await self.reads.read(ReadTranscript(str(self.comms.root), session_id))
        await self._publish(session_id, snapshot, client)

    async def subscribe(self, session_id: str, client: SocketClient) -> None:
        snapshot = await self.reads.read(SubscribeTranscript(str(self.comms.root), session_id))
        await self._publish(session_id, snapshot, client)

    async def _publish(
        self,
        session_id: str,
        snapshot: TranscriptSnapshotUpdate,
        destination: RuntimeServer | SocketClient,
    ) -> None:
        metadata = await Coordination.run_worker(partial(encode_updates, snapshot))
        await destination.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=metadata,
            ),
        )
