"""OpenCode's prompt and event boundary, using its native session identities."""
from __future__ import annotations

from typing import Annotated, ClassVar, Literal, TYPE_CHECKING

from metaclass_registry import RegistryConfig
from pydantic import Discriminator, JsonValue, Tag

from cli_comms import RegisteredWireMeta, WireModel

if TYPE_CHECKING:
    from opencode_comms_push import OpenCodeConnection


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


class UserMessage(WireModel):
    id: str
    sessionID: str
    role: Literal['user']


class SavedInput(WireModel):
    info: UserMessage


class PartHeader(WireModel):
    type: str
    sessionID: str
    messageID: str

    def matches(self, session: str, text: str) -> bool:
        return False


class TextPart(PartHeader):
    type: Literal['text']
    text: str

    def matches(self, session: str, text: str) -> bool:
        return self.sessionID == session and self.text == text


def part_tag(value: object) -> str:
    """Only text participates in input confirmation; other native parts are ignored."""
    part = value if isinstance(value, PartHeader) else PartHeader.model_validate(value)
    return 'text' if part.type == 'text' else 'other'


ConfirmationPart = Annotated[
    Annotated[TextPart, Tag('text')] | Annotated[PartHeader, Tag('other')],
    Discriminator(part_tag),
]


class PartUpdate(WireModel):
    sessionID: str
    part: ConfirmationPart


class NativeError(WireModel):
    name: str
    data: JsonValue


class SessionError(WireModel):
    sessionID: str | None = None
    error: NativeError | None = None


class EventHeader(WireModel):
    type: str


class OpenCodeEvent(WireModel, metaclass=RegisteredWireMeta):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    wire_tag: ClassVar[str]

    async def saved_input(self, connection: OpenCodeConnection, text: str) -> UserMessage | None:
        return None

    @classmethod
    def decode(cls, payload: str) -> OpenCodeEvent:
        header = EventHeader.model_validate_json(payload)
        family = cls.__registry_config__.registry_dict
        # The stream also carries unrelated session, permission and tool events.
        # Known events always use their full schema: malformed input is an error.
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
        # Confirm the containing message is a persisted user input. An assistant
        # repeating its text must never count as input delivery.
        return await connection.saved_input(part.messageID)


class Failed(OpenCodeEvent):
    wire_tag = 'session.error'
    properties: SessionError

    async def saved_input(self, connection: OpenCodeConnection, text: str) -> UserMessage | None:
        if self.properties.sessionID in (None, connection.session):
            raise RuntimeError(f'OpenCode session error: {self.properties.model_dump_json()}')
        return None
