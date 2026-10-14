"""OpenCode's session events drive both input confirmation and live output."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Literal, TYPE_CHECKING

from metaclass_registry import RegistryConfig
from pydantic import JsonValue, TypeAdapter

from .shared import (
    RegisteredWireMeta, TaggedWire, TranscriptUpdate, TurnStarted,
    TurnEnded, WireModel,
)
from .opencode_parts import MessageInfo, Part, SavedMessage, UserMessage

if TYPE_CHECKING:
    from .opencode import OpenCodeConnection


class TextInput(WireModel):
    type: Literal['text'] = 'text'
    text: str


class Prompt(WireModel):
    parts: tuple[TextInput, ...]


class Toast(WireModel):
    message: str
    variant: Literal['info', 'success', 'warning', 'error']
    title: str = 'Comms'


class Session(WireModel):
    id: str
    directory: str


class SavedInput(WireModel):
    info: UserMessage


class PartUpdate(WireModel):
    sessionID: str
    part: Part.declared_union()


class PartDelta(WireModel):
    sessionID: str
    messageID: str
    partID: str
    field: Literal['text']
    delta: str


class NativeError(WireModel):
    name: str
    data: JsonValue


class SessionError(WireModel):
    sessionID: str | None = None
    error: NativeError | None = None


class SessionStatus(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    @abstractmethod
    def publication(self) -> TranscriptUpdate: ...

    def receive(self, stream: OpenCodeStream) -> tuple[TranscriptUpdate, ...]:
        return (self.publication(),)


class IdleStatus(SessionStatus):
    wire_tag = 'idle'

    def publication(self) -> TranscriptUpdate:
        return TurnEnded()

    def receive(self, stream: OpenCodeStream) -> tuple[TranscriptUpdate, ...]:
        # All preceding parts have passed through this ordered subscription.
        stream.parts.clear()
        stream.messages.clear()
        stream.finished.clear()
        return super().receive(stream)


class BusyStatus(SessionStatus):
    wire_tag = 'busy'

    def publication(self) -> TranscriptUpdate:
        return TurnStarted()


class RetryStatus(SessionStatus):
    wire_tag = 'retry'
    attempt: int
    message: str
    next: int | float

    def publication(self) -> TranscriptUpdate:
        return TurnStarted(f'Retry {self.attempt}: {self.message}')


SESSION_STATUSES = TypeAdapter(dict[str, SessionStatus.declared_union()])
MESSAGES = TypeAdapter(list[SavedMessage])


class StatusUpdate(WireModel):
    sessionID: str
    status: SessionStatus.declared_union()


class MessageUpdate(WireModel):
    info: MessageInfo.declared_union()


class EventHeader(WireModel):
    type: str


class OpenCodeEvent(WireModel, metaclass=RegisteredWireMeta):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    wire_tag: ClassVar[str]

    async def saved_input(self, connection: OpenCodeConnection, text: str) -> UserMessage | None:
        return None

    async def receive(self, stream: OpenCodeStream) -> tuple[TranscriptUpdate, ...]:
        return ()

    @classmethod
    def decode(cls, payload: str) -> OpenCodeEvent:
        header = EventHeader.model_validate_json(payload)
        family = cls.__registry_config__.registry_dict
        # The native bus also carries projects, files and sessions outside this
        # subscription. Only declared event kinds participate in this contract.
        declaration = family[header.type] if header.type in family else cls
        return declaration.model_validate_json(payload)


class Connected(OpenCodeEvent):
    wire_tag = 'server.connected'
    type: Literal['server.connected']


class PartUpdated(OpenCodeEvent):
    wire_tag = 'message.part.updated'
    properties: PartUpdate

    async def saved_input(self, connection: OpenCodeConnection, text: str) -> UserMessage | None:
        part = self.properties.part
        if self.properties.sessionID != connection.session or not part.matches(connection.session, text):
            return None
        return await connection.saved_input(part.messageID)

    async def receive(self, stream: OpenCodeStream) -> tuple[TranscriptUpdate, ...]:
        if self.properties.sessionID != stream.connection.session:
            return ()
        return await stream.part(self.properties.part)


class PartChanged(OpenCodeEvent):
    wire_tag = 'message.part.delta'
    properties: PartDelta

    async def receive(self, stream: OpenCodeStream) -> tuple[TranscriptUpdate, ...]:
        delta = self.properties
        if delta.sessionID != stream.connection.session:
            return ()
        # An already-running part can predate this subscription. Its final whole
        # part is authoritative; a suffix alone cannot reconstruct its prefix.
        if delta.partID not in stream.parts or delta.partID in stream.finished:
            return ()
        part = stream.parts[delta.partID]
        if part.messageID != delta.messageID:
            raise ValueError('OpenCode delta changed its containing message')
        return stream.messages[part.messageID].append_text(part, delta.delta)


class MessageUpdated(OpenCodeEvent):
    wire_tag = 'message.updated'
    properties: MessageUpdate

    async def receive(self, stream: OpenCodeStream) -> tuple[TranscriptUpdate, ...]:
        info = self.properties.info
        if info.sessionID != stream.connection.session:
            return ()
        stream.messages[info.id] = info
        if info.time.completed is None:
            return ()
        # Completion also covers an item that began before we subscribed.
        saved = await stream.connection.message(info.id)
        return stream.snapshot(saved)


class StatusChanged(OpenCodeEvent):
    wire_tag = 'session.status'
    properties: StatusUpdate

    async def receive(self, stream: OpenCodeStream) -> tuple[TranscriptUpdate, ...]:
        if self.properties.sessionID != stream.connection.session:
            return ()
        return self.properties.status.receive(stream)


class Failed(OpenCodeEvent):
    wire_tag = 'session.error'
    properties: SessionError

    async def saved_input(self, connection: OpenCodeConnection, text: str) -> UserMessage | None:
        if self.properties.sessionID in (None, connection.session):
            raise RuntimeError(f'OpenCode session error: {self.properties.model_dump_json()}')
        return None


class OpenCodeStream:
    """One subscription assembles live parts; complete native parts own history."""

    def __init__(self, connection: OpenCodeConnection) -> None:
        self.connection = connection
        self.messages: dict[str, MessageInfo] = {}
        self.parts: dict[str, Part] = {}
        self.finished: set[str] = set()

    def snapshot(self, saved: SavedMessage) -> tuple[TranscriptUpdate, ...]:
        self.messages[saved.info.id] = saved.info
        updates: list[TranscriptUpdate] = []
        for part in saved.parts:
            if part.finished(saved.info):
                self.finished.add(part.id)
                self.parts.pop(part.id, None)
                updates.extend(saved.info.present(part))
        return tuple(updates)

    async def part(self, part: Part) -> tuple[TranscriptUpdate, ...]:
        if part.id in self.finished:
            return ()
        if part.messageID not in self.messages:
            saved = await self.connection.message(part.messageID)
            self.messages[saved.info.id] = saved.info
        info = self.messages[part.messageID]
        if part.finished(info):
            self.finished.add(part.id)
            self.parts.pop(part.id, None)
        else:
            self.parts[part.id] = part
        return info.present(part)
