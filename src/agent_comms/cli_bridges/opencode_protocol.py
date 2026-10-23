"""OpenCode's session events drive both input confirmation and live output."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from itertools import takewhile
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field, JsonValue

from agent_comms.cli_protocol import TaggedWire
from agent_comms.declared_family import UnknownFamilyMember
from agent_comms.runtime_updates import (
    AllTurnsEnded,
    AppendDraftItem,
    RuntimeUpdate,
    TurnEnded,
    TurnStarted,
)

from ..wire_models import DeclaredWireFamily, WireModel
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


class Revert(WireModel):
    """The message a user reverted to; it and everything after it were undone. A session
    nobody reverted undid nothing."""
    messageID: str = ''

    def kept(self, messages: Iterable[SavedMessage]) -> Iterator[SavedMessage]:
        return takewhile(lambda saved: saved.info.id != self.messageID, messages)


class Session(WireModel):
    id: str
    directory: str
    title: str | None = None
    revert: Revert = Field(default_factory=Revert)


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


class SessionStatus(TaggedWire, ABC, affix=""):

    @abstractmethod
    def publication(self) -> RuntimeUpdate: ...

    def receive(self, stream: OpenCodeStream) -> tuple[RuntimeUpdate, ...]:
        return (self.publication(),)


class IdleStatus(SessionStatus, declared_name='idle'):

    def publication(self) -> RuntimeUpdate:
        return TurnEnded()

    def receive(self, stream: OpenCodeStream) -> tuple[RuntimeUpdate, ...]:
        # All preceding parts have passed through this ordered subscription.
        stream.messages.clear()
        return (AllTurnsEnded(),)


class BusyStatus(SessionStatus, declared_name='busy'):

    def publication(self) -> RuntimeUpdate:
        return TurnStarted()


class RetryStatus(SessionStatus, declared_name='retry'):
    attempt: int
    message: str
    next: int | float

    def publication(self) -> RuntimeUpdate:
        return TurnStarted(f'Retry {self.attempt}: {self.message}')


SESSION_STATUSES = WireModel.adapter(dict[str, SessionStatus.declared_union()])
MESSAGES = WireModel.adapter(list[SavedMessage])


class StatusUpdate(WireModel):
    sessionID: str
    status: SessionStatus.declared_union()


class MessageUpdate(WireModel):
    info: MessageInfo.declared_union()


class EventHeader(WireModel):
    type: str


class OpenCodeEvent(DeclaredWireFamily, WireModel, affix=""):
    async def saved_input(self, connection: OpenCodeConnection, text: str) -> UserMessage | None:
        return None

    async def receive(self, stream: OpenCodeStream) -> tuple[RuntimeUpdate, ...]:
        return ()

    @classmethod
    def decode(cls, payload: str) -> OpenCodeEvent:
        header = EventHeader.model_validate_json(payload)
        # The native bus also carries projects, files and sessions outside this
        # subscription. Only declared event kinds participate in this contract.
        try:
            declaration = cls.member_named(header.type)
        except UnknownFamilyMember:
            declaration = cls
        return declaration.model_validate_json(payload)


class Connected(OpenCodeEvent, declared_name='server.connected'):
    type: Literal['server.connected']


class PartUpdated(OpenCodeEvent, declared_name='message.part.updated'):
    properties: PartUpdate

    async def saved_input(self, connection: OpenCodeConnection, text: str) -> UserMessage | None:
        part = self.properties.part
        if self.properties.sessionID != connection.session or not part.matches(connection.session, text):
            return None
        return await connection.saved_input(part.messageID)

    async def receive(self, stream: OpenCodeStream) -> tuple[RuntimeUpdate, ...]:
        if self.properties.sessionID != stream.connection.session:
            return ()
        return await stream.part(self.properties.part)


class PartChanged(OpenCodeEvent, declared_name='message.part.delta'):
    properties: PartDelta

    async def receive(self, stream: OpenCodeStream) -> tuple[RuntimeUpdate, ...]:
        delta = self.properties
        if delta.sessionID != stream.connection.session:
            return ()
        return (AppendDraftItem(delta.messageID, delta.partID, delta.delta),)


class MessageUpdated(OpenCodeEvent, declared_name='message.updated'):
    properties: MessageUpdate

    async def receive(self, stream: OpenCodeStream) -> tuple[RuntimeUpdate, ...]:
        info = self.properties.info
        if info.sessionID != stream.connection.session:
            return ()
        stream.messages[info.id] = info
        if info.time.completed is None:
            return ()
        # Completion also covers an item that began before we subscribed.
        saved = await stream.connection.message(info.id)
        return stream.snapshot(saved)


class StatusChanged(OpenCodeEvent, declared_name='session.status'):
    properties: StatusUpdate

    async def receive(self, stream: OpenCodeStream) -> tuple[RuntimeUpdate, ...]:
        if self.properties.sessionID != stream.connection.session:
            return ()
        return self.properties.status.receive(stream)


class Failed(OpenCodeEvent, declared_name='session.error'):
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

    def snapshot(self, saved: SavedMessage) -> tuple[RuntimeUpdate, ...]:
        self.know((saved,))
        return saved.publications()

    def know(self, saved: Iterable[SavedMessage]) -> None:
        """Parts that arrive later name only their message."""
        self.messages.update((message.info.id, message.info) for message in saved)

    async def part(self, part: Part) -> tuple[RuntimeUpdate, ...]:
        if part.messageID not in self.messages:
            saved = await self.connection.message(part.messageID)
            self.messages[saved.info.id] = saved.info
        info = self.messages[part.messageID]
        return info.present(part)
