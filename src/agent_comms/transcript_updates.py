"""Typed live ACP text and unconditional saved-history snapshot publication."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from acp.schema import AgentMessageChunk, TextContentBlock

from .acp_extension import (
    TurnChangedUpdate,
    TranscriptSnapshotUpdate,
    encode_updates,
)
from .comms import Comms
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .runtime import RuntimeServer, SocketClient
from .turn_lease import TurnState
from .ui_model.observation import ObservationProcess, ReadTranscript, SubscribeTranscript


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
    """Active transcript consumers, rather than the runtime, retain their shared reader."""

    def __init__(self, comms: Comms):
        self.comms = comms
        self._subscriptions: list[ObservationProcess] = []

    @contextmanager
    def observing(self) -> Iterator[ObservationProcess]:
        reads = (
            self._subscriptions[0]
            if self._subscriptions
            else ObservationProcess.awaited(self.comms.route)
        )
        self._subscriptions.append(reads)
        try:
            yield reads
        finally:
            self._subscriptions.remove(reads)
            if not self._subscriptions:
                reads.close()

    async def replay(self, session_id: str, client: RuntimeServer | SocketClient) -> None:
        with self.observing() as reads:
            snapshot = await reads.read(ReadTranscript(str(self.comms.root), session_id))
            await self._publish(session_id, snapshot, client)

    @asynccontextmanager
    async def subscribe(self, session_id: str, client: SocketClient) -> AsyncIterator[None]:
        with self.observing() as reads:
            snapshot = await reads.read(SubscribeTranscript(str(self.comms.root), session_id))
            await self._publish(session_id, snapshot, client)
            yield

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
