"""Typed live ACP text and unconditional saved-history snapshot publication."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from acp.schema import AgentMessageChunk, TextContentBlock

from .acp_extension import (
    TextRouteUpdate,
    TranscriptSnapshotUpdate,
    TurnStartedUpdate,
    encode_updates,
)
from .comms import Comms
from .declared_family import DeclaredFamily
from .routing import MessageRoute
from .runtime import RuntimeServer


@dataclass(frozen=True, kw_only=True)
class TranscriptUpdate(DeclaredFamily, affix="TranscriptUpdate"):
    @abstractmethod
    async def publish(self, session_id: str, client: Any) -> None: ...


@dataclass(frozen=True, kw_only=True)
class AgentTextTranscriptUpdate(TranscriptUpdate):
    text: str = ""
    route: MessageRoute | None = None

    async def publish(self, session_id: str, client: Any) -> None:
        if self.text:
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=self.text),
                    field_meta=encode_updates(TextRouteUpdate(self.route)),
                ),
            )


@dataclass(frozen=True, kw_only=True)
class StartedTranscriptUpdate(TranscriptUpdate):
    turn_id: str | None = None
    started_at: float | None = None
    activity: str | None = None
    activity_detail: str | None = None

    async def publish(self, session_id: str, client: Any) -> None:
        if self.turn_id is None:
            return
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(
                    TurnStartedUpdate(
                        self.turn_id, self.started_at, self.activity, self.activity_detail
                    )
                ),
            ),
        )


class TranscriptReplay:
    def __init__(self, comms: Comms, runtime: RuntimeServer):
        self.comms, self.runtime = comms, runtime

    async def replay(self, session_id: str, name: str, client: Any = None) -> None:
        destination = client or self.runtime
        page = await asyncio.to_thread(self.comms.transcripts.thread_transcript_page, name)
        await destination.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(TranscriptSnapshotUpdate(page)),
            ),
        )
