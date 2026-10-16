"""OpenCode's session events drive both input confirmation and live output."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Literal

from metaclass_registry import RegistryConfig
from pydantic import JsonValue, TypeAdapter

from agent_comms.cli_protocol import RegisteredWireMeta, TaggedWire, WireModel

from .opencode_parts import MessageInfo, Part, SavedMessage, UserMessage
from .shared import AllTurnsEnded, AppendDraftItem, TranscriptUpdate, TurnEnded, TurnStarted

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
        stream.messages.clear()
        return (AllTurnsEnded(),)


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
        return (AppendDraftItem(delta.messageID, delta.partID, delta.delta),)


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
    """Decode message/part metadata; the shared output assembles live text."""

    def __init__(self, connection: OpenCodeConnection) -> None:
        self.connection = connection
        self.messages: dict[str, MessageInfo] = {}

    def snapshot(self, saved: SavedMessage) -> tuple[TranscriptUpdate, ...]:
        self.messages[saved.info.id] = saved.info
        updates: list[TranscriptUpdate] = []
        for part in saved.parts:
            if part.finished(saved.info):
                updates.extend(saved.info.present(part))
        return tuple(updates)

    async def part(self, part: Part) -> tuple[TranscriptUpdate, ...]:
        if part.messageID not in self.messages:
            saved = await self.connection.message(part.messageID)
            self.messages[saved.info.id] = saved.info
        info = self.messages[part.messageID]
        return info.present(part)
