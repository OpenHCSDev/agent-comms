"""Typed ACP replay boundary for existing saved-history projections.

Snapshot clients retain the existing full TranscriptPage representation. Legacy
text clients consume this declaration family; unsupported saved event kinds
remain silent, as before, without contaminating the live A10 event stream.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from dataclasses import asdict, dataclass, fields
from typing import Any, Self

from acp.schema import AgentMessageChunk, TextContentBlock, UserMessageChunk

from .declarations import MessageRoute
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .operations import Comms, TranscriptEvent
from .runtime import RuntimeServer


@dataclass(frozen=True, kw_only=True)
class TranscriptUpdate(DeclaredFamily, affix="TranscriptUpdate"):
    @abstractmethod
    async def publish(self, session_id: str, client: Any) -> None: ...

    @classmethod
    def from_legacy(cls, value: dict[str, Any]) -> Self:
        try:
            member = cls.decode(value.get("type"))
        except ValueError:
            return IgnoredTranscriptUpdate()
        payload = {"kind": member.declared_name}
        for declared in fields(member):
            if declared.name in value:
                payload[declared.name] = FieldCodec.encode(value[declared.name])
        return FieldCodec.decode(cls, payload)

    @classmethod
    def from_transcript(cls, event: TranscriptEvent) -> TranscriptUpdate:
        return cls.from_legacy(
            {
                "type": event.kind,
                "text": event.text,
                "route": event.routing.reply if event.routing else None,
            }
        )


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


class AgentTextPublication:
    async def publish(self, session_id: str, client: Any) -> None:
        if self.text:
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=self.text),
                    field_meta={
                        "agentComms": {"route": asdict(self.route) if self.route else None}
                    },
                ),
            )


@dataclass(frozen=True, kw_only=True)
class AssistantTranscriptUpdate(AgentTextPublication, AgentTextTranscriptUpdate):
    pass


@dataclass(frozen=True, kw_only=True)
class NoticeTranscriptUpdate(AgentTextPublication, AgentTextTranscriptUpdate):
    pass


@dataclass(frozen=True, kw_only=True)
class SentTranscriptUpdate(AgentTextPublication, AgentTextTranscriptUpdate):
    pass


@dataclass(frozen=True, kw_only=True)
class StartedTranscriptUpdate(TranscriptUpdate):
    turn_id: str | None = None
    started_at: float | None = None
    activity: str | None = None
    activity_detail: str | None = None

    async def publish(self, session_id: str, client: Any) -> None:
        lifecycle = {
            "turnStarted": True,
            "turnId": self.turn_id,
            **({"startedAt": self.started_at} if self.started_at is not None else {}),
            **({"activity": self.activity} if self.activity is not None else {}),
            **(
                {"activityDetail": self.activity_detail} if self.activity_detail is not None else {}
            ),
        }
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": lifecycle},
            ),
        )


class TranscriptReplay:
    def __init__(self, comms: Comms, runtime: RuntimeServer):
        self.comms, self.runtime = comms, runtime
        self.snapshots = False
        self.diffs = False

    async def replay(
        self,
        session_id: str,
        name: str,
        client: Any = None,
        *,
        snapshots: bool | None = None,
        diffs: bool | None = None,
    ) -> None:
        use_snapshots = (
            (self.snapshots if client is None else getattr(client, "transcript_snapshots", False))
            if snapshots is None
            else snapshots is True
        )
        destination = client or self.runtime
        if use_snapshots:
            page = await asyncio.to_thread(self.comms.thread_transcript_page, name)
            include_diff = (
                (self.diffs if client is None else getattr(client, "transcript_diffs", False))
                if diffs is None
                else diffs is True
            )
            await destination.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={
                        "agentComms": {
                            "transcript": [
                                event.to_wire(include_diff=include_diff) for event in page.events
                            ],
                            "transcriptPage": page.metadata(),
                        }
                    },
                ),
            )
            return
        events = await asyncio.to_thread(self.comms.thread_transcript, name)
        for event in events:
            await TranscriptUpdate.from_transcript(event).publish(session_id, destination)
