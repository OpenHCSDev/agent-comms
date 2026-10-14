"""Codex's JSON protocol boundary and its transcript meanings.

Wire tags belong to Codex. Each decoded item supplies its own Core presentation;
consumers neither inspect JSON fields nor rediscover what kind of item it is.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
import json
from typing import Annotated, ClassVar, Generic, Literal, TypeVar, Union

from metaclass_registry import RegistryConfig
from pydantic import (
    Discriminator, Field, JsonValue, Tag, TypeAdapter,
    computed_field,
)

from agent_comms.activity import ActivityState
from agent_comms.native_tools import NativeTool
from agent_comms.transcript_events import (
    AssistantTranscript, ThinkingTranscript, ToolStartTranscript, ToolEndTranscript,
    TranscriptEvent, UserTranscript, NoticeTranscript,
)


from .shared import (
    ActiveItem, FinishedItem, RegisteredWireMeta, TranscriptUpdate,
    TurnEnded, TurnStarted, WireModel,
)


class TaggedWire(WireModel, ABC, metaclass=RegisteredWireMeta):
    """A Codex object whose JSON ``type`` names its declared class."""
    wire_tag: ClassVar[str]

    @computed_field
    @property
    def type(self) -> str:
        return self.wire_tag

    @classmethod
    def declared_union(cls) -> object:
        """The wire grammar of every declared member, discriminated by its tag."""
        def tag(value: object) -> str:
            if isinstance(value, cls):
                return value.wire_tag
            return WireHeader.model_validate(value).type
        return Annotated[
            Union[tuple(Annotated[member, Tag(member.wire_tag)]
                        for member in cls.__registry_config__.registry_dict.values())],
            Discriminator(tag),
        ]


class WireHeader(WireModel):
    type: str


class UserInput(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True,
    )

    @property
    @abstractmethod
    def display(self) -> str: ...


class TextInput(UserInput):
    wire_tag = 'text'
    text: str

    @property
    def display(self) -> str:
        return self.text


class ImageInput(UserInput):
    wire_tag = 'image'

    @property
    def display(self) -> str:
        return 'Image'


class LocalImageInput(UserInput):
    wire_tag = 'localImage'
    path: str

    @property
    def display(self) -> str:
        return f'Image: {self.path}'


class AudioInput(UserInput):
    wire_tag = 'audio'
    url: str

    @property
    def display(self) -> str:
        return 'Audio'


class LocalAudioInput(UserInput):
    wire_tag = 'localAudio'
    path: str

    @property
    def display(self) -> str:
        return f'Audio: {self.path}'


class SkillInput(UserInput):
    wire_tag = 'skill'
    name: str
    path: str

    @property
    def display(self) -> str:
        return f'Skill: {self.name}'


class MentionInput(UserInput):
    wire_tag = 'mention'
    name: str
    path: str

    @property
    def display(self) -> str:
        return f'@{self.name}'


CodexUserInput = UserInput.declared_union()


class RequestParams(WireModel):
    method: ClassVar[str]


class TurnInput(RequestParams):
    method = 'turn/start'
    threadId: str
    clientUserMessageId: str


class MessageInput(TurnInput):
    input: tuple[TextInput, ...] = Field(min_length=1)


class BusInput(MessageInput):
    client_prefix: ClassVar[str] = 'comms:'

    @classmethod
    def for_message(cls, thread: str, message_id: str, text: str) -> BusInput:
        return cls(threadId=thread, clientUserMessageId=cls.client_prefix + message_id,
                   input=(TextInput(text=text),))

    @classmethod
    def identifies(cls, client_id: str | None) -> bool:
        return client_id is not None and client_id.startswith(cls.client_prefix)


SEND_TOOL = 'comms_send'


class ToolOutput(WireModel):
    name: Literal[SEND_TOOL] = SEND_TOOL
    output: str


class DisplayOutput(TurnInput):
    input: tuple[()] = ()
    toolOutput: ToolOutput


class ClientInfo(WireModel):
    name: str = 'agent_comms_push'
    version: str = '2'


class Capabilities(WireModel):
    experimentalApi: bool = True
    optOutNotificationMethods: tuple[str, ...] = ()


class Initialize(RequestParams):
    method = 'initialize'
    clientInfo: ClientInfo = Field(default_factory=ClientInfo)
    capabilities: Capabilities = Field(default_factory=Capabilities)


class ResumeThread(RequestParams):
    method = 'thread/resume'
    threadId: str
    excludeTurns: bool = True


Params = TypeVar('Params', bound=RequestParams)


class Request(WireModel, Generic[Params]):
    id: int
    method: str
    params: Params


class Initialized(WireModel):
    method: Literal['initialized'] = 'initialized'


class RpcError(WireModel):
    code: int
    message: str
    data: JsonValue = None


class RpcSuccess(WireModel):
    id: int
    result: JsonValue


class RpcFailure(WireModel):
    id: int | None
    error: RpcError


class RpcNotification(WireModel):
    method: str
    params: JsonValue


RPC_RESPONSE = TypeAdapter(RpcSuccess | RpcFailure | RpcNotification)


class Item(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag',
        skip_if_no_key=True,
    )
    id: str
    # What the CLI is doing while it produces this item.
    activity: ClassVar[ActivityState] = ActivityState.WORKING
    doing: ClassVar[str] = ''

    @abstractmethod
    def events(self) -> tuple[TranscriptEvent, ...]:
        """The finished item's presentation."""

    @property
    def activity_detail(self) -> str:
        return self.doing

class TextBody(WireModel):
    text: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (AssistantTranscript(self.text),)


class AssistantItem(TextBody, Item):
    wire_tag = 'agentMessage'
    doing = 'Replying'


class PlanItem(TextBody, Item):
    wire_tag = 'plan'
    doing = 'Planning'


class ReasoningItem(Item):
    wire_tag = 'reasoning'
    activity = ActivityState.THINKING
    summary: list[str] = Field(default_factory=list)

    def events(self) -> tuple[TranscriptEvent, ...]:
        # Only public summaries; private/raw reasoning is never projected.
        text = '\n\n'.join(part for part in self.summary if part)
        return (ThinkingTranscript(text),) if text else ()


class UserItem(Item):
    wire_tag = 'userMessage'
    content: list[CodexUserInput]
    clientId: str | None = None

    def events(self) -> tuple[TranscriptEvent, ...]:
        # Bus inputs already appear in Core's inbox. Do not echo them as new input.
        if BusInput.identifies(self.clientId):
            return ()
        return (UserTranscript('\n'.join(part.display for part in self.content)),)


class ToolItem(Item, ABC):
    @property
    @abstractmethod
    def tool_name(self) -> str: ...

    @property
    @abstractmethod
    def arguments(self) -> JsonValue: ...

    @property
    @abstractmethod
    def output(self) -> str: ...

    @property
    @abstractmethod
    def succeeded(self) -> bool: ...

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (ToolStartTranscript(tool_call_id=self.id, tool_name=self.tool_name,
                                    raw_input=self.arguments),
                ToolEndTranscript(tool_call_id=self.id, tool_name=self.tool_name,
                                  text=self.output, ok=self.succeeded))

    @property
    def activity_detail(self) -> str:
        # Core's tool declarations own the action text native tools show.
        arguments = self.arguments if isinstance(self.arguments, dict) else {}
        return NativeTool.start(self.id, self.tool_name, arguments).title


class CommandItem(ToolItem):
    wire_tag = 'commandExecution'
    command: str
    status: str
    aggregatedOutput: str | None = None
    exitCode: int | None = None

    @property
    def tool_name(self) -> str:
        return 'bash'  # Core's declared shell tool, which presents its command

    @property
    def arguments(self) -> JsonValue:
        return {'command': self.command}

    @property
    def output(self) -> str:
        return self.aggregatedOutput or ''

    @property
    def succeeded(self) -> bool:
        return self.status == 'completed' and self.exitCode == 0


class ToolContentPart(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True,
    )

    @property
    @abstractmethod
    def display(self) -> str: ...


class ToolText(ToolContentPart):
    wire_tag = 'inputText'
    text: str

    @property
    def display(self) -> str:
        return self.text


class ToolImage(ToolContentPart):
    wire_tag = 'inputImage'
    imageUrl: str

    @property
    def display(self) -> str:
        return '[Image output]'


class ToolAudio(ToolContentPart):
    wire_tag = 'inputAudio'
    audioUrl: str

    @property
    def display(self) -> str:
        return '[Audio output]'


ToolContent = ToolContentPart.declared_union()


class DynamicToolItem(ToolItem):
    wire_tag = 'dynamicToolCall'
    tool: str
    input_value: JsonValue = Field(alias='arguments')
    contentItems: list[ToolContent] | None = None
    success: bool | None = None

    @property
    def tool_name(self) -> str:
        return self.tool

    @property
    def arguments(self) -> JsonValue:
        return self.input_value

    @property
    def output(self) -> str:
        return '\n'.join(part.display for part in self.contentItems or ())

    @property
    def succeeded(self) -> bool:
        if self.success is None:
            raise ValueError('Completed dynamic tool has no success result')
        return self.success


class McpResult(WireModel):
    # App-server explicitly declares each MCP result content as arbitrary JSON.
    # Preserve that external value instead of inventing nullable content fields.
    content: list[JsonValue]
    structuredContent: JsonValue = None
    isError: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.isError


class McpError(WireModel):
    message: str


class McpToolItem(ToolItem):
    wire_tag = 'mcpToolCall'
    server: str
    tool: str
    input_value: JsonValue = Field(alias='arguments')
    status: str
    result: McpResult | None = None
    error: McpError | None = None

    @property
    def tool_name(self) -> str:
        return f'{self.server}/{self.tool}'

    @property
    def arguments(self) -> JsonValue:
        return self.input_value

    @property
    def output(self) -> str:
        if self.error is not None:
            return self.error.message
        if self.result is None:
            return ''
        return json.dumps(self.result.content, ensure_ascii=False)

    @property
    def succeeded(self) -> bool:
        if self.status == 'inProgress':
            raise ValueError('MCP tool completed notification still says inProgress')
        return self.status == 'completed' and self.error is None and (
            self.result is None or self.result.succeeded)


class SendOutputItem(Item):
    wire_tag = 'functionCallOutput'
    name: str
    output: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        # The bridge's send result is already rendered from the bus in Toad.
        if self.name == SEND_TOOL:
            return ()
        return (ToolEndTranscript(tool_call_id=self.id, tool_name=self.name, text=self.output),)


class CompactionItem(Item):
    wire_tag = 'contextCompaction'
    doing = 'Compacting context'

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript('Context compacted.'),)


class HookPromptItem(Item):
    wire_tag = 'hookPrompt'

    def events(self) -> tuple[TranscriptEvent, ...]:
        # Harness instructions are not assistant output.
        return ()


class ReviewBody(WireModel):
    review: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(self.review),)


class EnteredReviewItem(ReviewBody, Item):
    wire_tag = 'enteredReviewMode'


class ExitedReviewItem(ReviewBody, Item):
    wire_tag = 'exitedReviewMode'


class SleepItem(Item):
    wire_tag = 'sleep'
    durationMs: int

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Waited ({self.durationMs} ms requested).'),)

    @property
    def activity_detail(self) -> str:
        return f'Waiting {self.durationMs} ms'


class ImageViewItem(Item):
    wire_tag = 'imageView'
    path: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Image: {self.path}'),)


class ImageGenerationItem(Item):
    wire_tag = 'imageGeneration'
    status: str
    result: str
    savedPath: str | None = None

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Image generation: {self.status}' +
                                 (f' — {self.savedPath}' if self.savedPath else '')),)


class WebSearchItem(Item):
    wire_tag = 'webSearch'
    query: str
    action: JsonValue = None
    results: JsonValue = None

    def events(self) -> tuple[TranscriptEvent, ...]:
        # Search status carries no success flag. Do not invent a successful end.
        return (NoticeTranscript(f'Web search: {self.query}'),)

    @property
    def activity_detail(self) -> str:
        return f'Searching: {self.query}'


class FileChange(WireModel):
    path: str
    diff: str
    kind: JsonValue


class FileChangeItem(Item):
    wire_tag = 'fileChange'
    doing = 'Editing files'
    changes: list[FileChange]
    status: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript('File changes: ' + self.status + '\n' +
                                 '\n'.join(f'{change.path}\n{change.diff}' for change in self.changes)),)


class AgentState(WireModel):
    status: str
    message: str | None = None


class AgentToolItem(Item):
    wire_tag = 'collabAgentToolCall'
    tool: str
    status: str
    receiverThreadIds: list[str]
    agentsStates: dict[str, AgentState]

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'{self.tool}: {self.status}\n' + '\n'.join(
            f'{name}: {state.status}' + (f' — {state.message}' if state.message else '')
            for name, state in self.agentsStates.items())),)


class AgentActivityItem(Item):
    wire_tag = 'subAgentActivity'
    agentPath: str
    agentThreadId: str
    kind: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'{self.agentPath}: {self.kind}'),)


# The existing registration owner derives this wire grammar from declarations.
# A new item supplies its tag and behavior once, without a second decoder edit.
CodexItem = Item.declared_union()


class ItemsList(RequestParams):
    method = 'thread/items/list'
    threadId: str
    limit: int = 20
    sortDirection: Literal['desc'] = 'desc'
    cursor: str | None = None


class ItemEntry(WireModel):
    turnId: str
    item: CodexItem
    startedAtMs: int | None = None
    completedAtMs: int | None = None

    @property
    def completed(self) -> bool:
        return self.completedAtMs is not None

    def publication(self) -> FinishedItem:
        if self.completedAtMs is None:
            raise ValueError(f'Item {self.item.id} has not finished')
        timestamp = self.startedAtMs or self.completedAtMs
        return FinishedItem(self.turnId, self.item.id, tuple(
            replace(event, timestamp=timestamp / 1000) for event in self.item.events()))


class ItemsPage(WireModel):
    data: list[ItemEntry]
    nextCursor: str | None = None


class TranscriptNotification(WireModel, ABC, metaclass=RegisteredWireMeta):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='method', skip_if_no_key=True)
    method: ClassVar[str]
    threadId: str

    @abstractmethod
    def apply(self, stream: ItemStream) -> TranscriptUpdate | None: ...


class TurnHeader(WireModel):
    id: str


class TurnBegan(TranscriptNotification):
    method = 'turn/started'
    turn: TurnHeader

    def apply(self, stream: ItemStream) -> TranscriptUpdate:
        return TurnStarted(self.turn.id)


class TurnFinished(TranscriptNotification):
    method = 'turn/completed'
    turn: TurnHeader

    def apply(self, stream: ItemStream) -> TranscriptUpdate:
        for key in [key for key in stream.started if key[0] == self.turn.id]:
            del stream.started[key]
        return TurnEnded(self.turn.id)


class ItemNotification(TranscriptNotification, ABC):
    turnId: str


class Started(ItemNotification):
    method = 'item/started'
    item: CodexItem
    startedAtMs: int

    def apply(self, stream: ItemStream) -> TranscriptUpdate:
        stream.started[self.turnId, self.item.id] = self.startedAtMs
        return ActiveItem(self.turnId, self.item.id, self.item.activity, self.item.activity_detail)


class Completed(ItemNotification):
    method = 'item/completed'
    item: CodexItem
    completedAtMs: int

    def apply(self, stream: ItemStream) -> TranscriptUpdate:
        # A completed notification carries the whole item; deltas are not needed.
        return ItemEntry(turnId=self.turnId, item=self.item,
                         startedAtMs=stream.started.pop((self.turnId, self.item.id), None),
                         completedAtMs=self.completedAtMs).publication()


class ItemStream:
    """When each item in progress started; finished items arrive whole."""

    def __init__(self, thread: str):
        self.thread = thread
        self.started: dict[tuple[str, str], int] = {}

    def receive(self, notification: RpcNotification) -> TranscriptUpdate | None:
        declarations = TranscriptNotification.__registry_config__.registry_dict
        if notification.method not in declarations:
            # Deltas, usage, account, raw/private reasoning and other control
            # events are not transcript content. Known methods declare a type.
            return None
        event = declarations[notification.method].model_validate(notification.params)
        if event.threadId != self.thread:
            return None
        return event.apply(self)
