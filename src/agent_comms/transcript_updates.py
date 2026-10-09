"""Typed ACP replay boundary for existing saved-history projections.

Snapshot clients receive typed saved presentation facts. Standard ACP text
clients receive each fact's declared replay update; silent presentation facts
do not enter the live event stream.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

from acp.schema import AgentMessageChunk, TextContentBlock, UserMessageChunk

from .acp_extension import TextRouteUpdate, TurnStartedUpdate, encode_updates
from .comms import Comms
from .declared_family import DeclaredFamily
from .routing import MessageRoute
from .runtime import RuntimeServer
from .transcript_events import TranscriptCodec


@dataclass(frozen=True, kw_only=True)
class TranscriptUpdate(DeclaredFamily, affix="TranscriptUpdate"):
    @abstractmethod
    async def publish(self, session_id: str, client: Any) -> None: ...


@dataclass(frozen=True, kw_only=True)
class IgnoredTranscriptUpdate(TranscriptUpdate):
    async def publish(self, session_id: str, client: Any) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class UserTranscriptUpdate(TranscriptUpdate):
    text: str = ""

    async def publish(self, session_id: str, client: Any) -> None:
        if self.text:
            await client.session_update(
                session_id=session_id,
                update=UserMessageChunk(
                    session_update="user_message_chunk",
                    content=TextContentBlock(type="text", text=self.text),
                ),
            )


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
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(TurnStartedUpdate(
                    self.turn_id, self.started_at, self.activity, self.activity_detail
                )),
            ),
        )


class TranscriptReplay:
    def __init__(self, comms: Comms, runtime: RuntimeServer):
        self.comms, self.runtime = comms, runtime
        self.snapshots = False

    async def replay(
        self,
        session_id: str,
        name: str,
        client: Any = None,
        *,
        snapshots: bool | None = None,
    ) -> None:
        use_snapshots = (
            (self.snapshots if client is None else getattr(client, "transcript_snapshots", False))
            if snapshots is None
            else snapshots is True
        )
        destination = client or self.runtime
        if use_snapshots:
            page = await asyncio.to_thread(self.comms.transcripts.thread_transcript_page, name)
            await destination.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={
                        "agentComms": {
                            "transcript": [TranscriptCodec.encode(event) for event in page.events],
                            "transcriptPage": page.metadata(),
                        }
                    },
                ),
            )
            return
        events = await asyncio.to_thread(self.comms.transcripts.thread_transcript, name)
        for event in events:
            await event.replay_update().publish(session_id, destination)
