"""Typed live ACP text and unconditional saved-history snapshot publication."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
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
from .runtime import RuntimeServer
from .turn_lease import TurnState


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

    async def replay(self, session_id: str, name: str, client: Any = None) -> None:
        destination = client or self.runtime
        snapshot = await asyncio.to_thread(
            TranscriptSnapshotUpdate.capture, self.comms.transcripts, name
        )
        metadata = await Coordination.run_worker(partial(encode_updates, snapshot))
        await destination.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=metadata,
            ),
        )
