"""Codex's JSON protocol boundary and its transcript meanings.

Wire tags belong to Codex. Each decoded item supplies its own Core presentation;
consumers neither inspect JSON fields nor rediscover what kind of item it is.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
import json
import sys
from typing import Annotated, ClassVar, Generic, Literal, TypeVar, Union

from metaclass_registry import RegistryConfig
from pydantic import (
    Discriminator, Field, JsonValue, Tag, TypeAdapter,
    computed_field,
)

from agent_comms.transcript_events import (
    AssistantTranscript, ThinkingTranscript, ToolStartTranscript, ToolEndTranscript,
    TranscriptEvent, UserTranscript, NoticeTranscript,
)


from cli_comms import RegisteredWireMeta, WireModel, TranscriptUpdate


class TextInput(WireModel):
    type: Literal['text'] = 'text'
    text: str
    text_elements: tuple[JsonValue, ...] = ()


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


class ToolOutput(WireModel):
    name: Literal['comms_send'] = 'comms_send'
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


class Item(WireModel, ABC, metaclass=RegisteredWireMeta):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag',
        skip_if_no_key=True,
    )
    wire_tag: ClassVar[str]
    id: str

    @computed_field
    @property
    def type(self) -> str:
        return self.wire_tag

    @abstractmethod
    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]: ...

class TextBody(WireModel):
    text: str

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (AssistantTranscript(self.text),)

    def append_text(self, text: str) -> None:
        self.text += text


class AssistantItem(TextBody, Item):
    wire_tag = 'agentMessage'


class PlanItem(TextBody, Item):
    wire_tag = 'plan'


class ReasoningItem(Item):
    wire_tag = 'reasoning'
    summary: list[str] = Field(default_factory=list)

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        # Only public summaries; private/raw reasoning is never projected.
        return (ThinkingTranscript('\n\n'.join(self.summary)),)

    def append_summary(self, index: int, text: str) -> None:
        if index == len(self.summary):
            self.summary.append('')
        if not 0 <= index < len(self.summary):
            raise ValueError('Reasoning summary arrived out of order')
        self.summary[index] += text


class UserItem(Item):
    wire_tag = 'userMessage'
    content: list[TextInput]
    clientId: str | None = None

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        # Bus inputs already appear in Core's inbox. Do not echo them as new input.
        if BusInput.identifies(self.clientId):
            return ()
        return (UserTranscript('\n'.join(part.text for part in self.content)),)


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

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        start = ToolStartTranscript(tool_call_id=self.id, tool_name=self.tool_name,
                                    raw_input=self.arguments)
        if not completed:
            return (start,)
        return (start, ToolEndTranscript(tool_call_id=self.id, tool_name=self.tool_name,
                                        text=self.output, ok=self.succeeded))


class CommandItem(ToolItem):
    wire_tag = 'commandExecution'
    command: str
    status: str
    aggregatedOutput: str | None = None
    exitCode: int | None = None

    @property
    def tool_name(self) -> str:
        return 'shell'

    @property
    def arguments(self) -> JsonValue:
        return self.command

    @property
    def output(self) -> str:
        return self.aggregatedOutput or ''

    @property
    def succeeded(self) -> bool:
        return self.status == 'completed' and self.exitCode == 0

    def append_output(self, text: str) -> None:
        self.aggregatedOutput = self.output + text


class ToolText(WireModel):
    type: Literal['inputText']
    text: str

    @property
    def display(self) -> str:
        return self.text


class ToolImage(WireModel):
    type: Literal['inputImage']
    imageUrl: str

    @property
    def display(self) -> str:
        return '[Image output]'


class ToolAudio(WireModel):
    type: Literal['inputAudio']
    audioUrl: str

    @property
    def display(self) -> str:
        return '[Audio output]'


ToolContent = Annotated[ToolText | ToolImage | ToolAudio, Field(discriminator='type')]


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
            self.result is None or not self.result.isError)


class SendOutputItem(Item):
    wire_tag = 'functionCallOutput'
    name: str
    output: str

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        # The bridge's send result is already rendered from the bus in Toad.
        if self.name == ToolOutput.model_fields['name'].default:
            return ()
        return (ToolEndTranscript(tool_call_id=self.id, tool_name=self.name, text=self.output),)


class CompactionItem(Item):
    wire_tag = 'contextCompaction'

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript('Context compacted.' if completed else 'Compacting context…'),)


class HookPromptItem(Item):
    wire_tag = 'hookPrompt'

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        # Harness instructions are not assistant output.
        return ()


class ReviewBody(WireModel):
    review: str

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(self.review),)


class EnteredReviewItem(ReviewBody, Item):
    wire_tag = 'enteredReviewMode'


class ExitedReviewItem(ReviewBody, Item):
    wire_tag = 'exitedReviewMode'


class SleepItem(Item):
    wire_tag = 'sleep'
    durationMs: int

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f"Wait {'ended' if completed else 'started'} ({self.durationMs} ms requested)."),)


class ImageViewItem(Item):
    wire_tag = 'imageView'
    path: str

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Image: {self.path}'),)


class ImageGenerationItem(Item):
    wire_tag = 'imageGeneration'
    status: str
    result: str
    savedPath: str | None = None

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Image generation: {self.status}' +
                                 (f' — {self.savedPath}' if self.savedPath else '')),)


class WebSearchItem(Item):
    wire_tag = 'webSearch'
    query: str
    action: JsonValue = None
    results: JsonValue = None

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        # Search status carries no success flag. Do not invent a successful end.
        return (NoticeTranscript(f"Web search {'finished' if completed else 'started'}: {self.query}"),)


class FileChange(WireModel):
    path: str
    diff: str
    kind: JsonValue


class FileChangeItem(Item):
    wire_tag = 'fileChange'
    changes: list[FileChange]
    status: str

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
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

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'{self.tool}: {self.status}\n' + '\n'.join(
            f'{name}: {state.status}' + (f' — {state.message}' if state.message else '')
            for name, state in self.agentsStates.items())),)


class AgentActivityItem(Item):
    wire_tag = 'subAgentActivity'
    agentPath: str
    agentThreadId: str
    kind: str

    def events(self, *, completed: bool) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'{self.agentPath}: {self.kind}'),)


# The existing registration owner derives this wire grammar from declarations.
# A new item supplies its tag and behavior once, without a second decoder edit.
class ItemHeader(WireModel):
    type: str


def item_tag(value: object) -> str:
    if isinstance(value, Item):
        return value.wire_tag
    return ItemHeader.model_validate(value).type


CodexItem = Annotated[
    Union[tuple(Annotated[member, Tag(member.wire_tag)]
                for member in Item.__registry_config__.registry_dict.values())],
    Discriminator(item_tag),
]


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

    def publication(self) -> TranscriptUpdate:
        timestamp = self.startedAtMs or self.completedAtMs
        return TranscriptUpdate(self.turnId, self.item.id, tuple(
            replace(event, timestamp=timestamp / 1000 if timestamp is not None else None)
            for event in self.item.events(completed=self.completedAtMs is not None)))


class ItemsPage(WireModel):
    data: list[ItemEntry]
    nextCursor: str | None = None


class TranscriptNotification(WireModel, ABC, metaclass=RegisteredWireMeta):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='method', skip_if_no_key=True)
    method: ClassVar[str]
    threadId: str
    turnId: str

    @abstractmethod
    def apply(self, stream: ItemStream) -> TranscriptUpdate | None: ...


class Started(TranscriptNotification):
    method = 'item/started'
    item: CodexItem
    startedAtMs: int

    def apply(self, stream: ItemStream) -> TranscriptUpdate:
        entry = ItemEntry(turnId=self.turnId, item=self.item, startedAtMs=self.startedAtMs)
        stream.active[self.turnId, self.item.id] = entry
        return entry.publication()


class Completed(TranscriptNotification):
    method = 'item/completed'
    item: CodexItem
    completedAtMs: int

    def apply(self, stream: ItemStream) -> TranscriptUpdate:
        key = self.turnId, self.item.id
        previous = stream.active.pop(key, None)
        stream.incomplete.discard(key)
        return ItemEntry(turnId=self.turnId, item=self.item,
                         startedAtMs=previous.startedAtMs if previous else None,
                         completedAtMs=self.completedAtMs).publication()


class ItemDelta(TranscriptNotification, ABC):
    itemId: str
    delta: str

    @abstractmethod
    def append(self, item: Item) -> None: ...

    def apply(self, stream: ItemStream) -> TranscriptUpdate | None:
        key = self.turnId, self.itemId
        if key not in stream.active:
            # Connecting midway through an item provides no initial content.
            # Its full completion is authoritative; never publish a suffix as
            # though it were the whole item or replay a possibly included delta.
            if key not in stream.incomplete:
                print(f'Joined during item {self.itemId}; waiting for its complete snapshot',
                      file=sys.stderr, flush=True)
                stream.incomplete.add(key)
            return None
        entry = stream.active[key]
        self.append(entry.item)
        return entry.publication()


class TextAppend(ItemDelta, ABC):
    def append(self, item: Item) -> None:
        if not isinstance(item, TextBody):
            raise TypeError('Text delta names an item without text content')
        item.append_text(self.delta)


class AssistantDelta(TextAppend):
    method = 'item/agentMessage/delta'


class PlanDelta(TextAppend):
    method = 'item/plan/delta'


class ReasoningDelta(ItemDelta):
    method = 'item/reasoning/summaryTextDelta'
    summaryIndex: int

    def append(self, item: Item) -> None:
        if not isinstance(item, ReasoningItem):
            raise TypeError('Summary delta names a non-reasoning item')
        item.append_summary(self.summaryIndex, self.delta)


class CommandDelta(ItemDelta):
    method = 'item/commandExecution/outputDelta'

    def append(self, item: Item) -> None:
        if not isinstance(item, CommandItem):
            raise TypeError('Command output names a non-command item')
        item.append_output(self.delta)


class ItemStream:
    """Only active item assembly; Core owns durable transcript replacement."""

    def __init__(self, thread: str):
        self.thread = thread
        self.active: dict[tuple[str, str], ItemEntry] = {}
        self.incomplete: set[tuple[str, str]] = set()

    def receive(self, notification: RpcNotification) -> TranscriptUpdate | None:
        declarations = TranscriptNotification.__registry_config__.registry_dict
        if notification.method not in declarations:
            # Usage, account, raw/private reasoning and other control events
            # are not transcript content. Known content methods declare a type.
            return None
        event = declarations[notification.method].model_validate(notification.params)
        if event.threadId != self.thread:
            return None
        return event.apply(self)
