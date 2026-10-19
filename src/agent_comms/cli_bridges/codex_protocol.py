"""Codex's JSON protocol boundary and its transcript meanings.

Wire tags belong to Codex. Each decoded item supplies its own Core presentation;
consumers neither inspect JSON fields nor rediscover what kind of item it is.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Annotated, ClassVar, Generic, Literal, TypeVar

from metaclass_registry import RegistryConfig
from pydantic import (
    Field,
    JsonValue,
    TypeAdapter,
    ValidateAs,
)

from agent_comms.activity import ActivityState
from agent_comms.cli_protocol import RegisteredWireMeta, TaggedWire, WireModel
from agent_comms.external_transcript import EarlierPage, NoEarlierPage
from agent_comms.native_tools import FilePatchTool
from agent_comms.recorded_time import EventTime, UnrecordedEventTime
from agent_comms.runtime_updates import (
    AppendDraftItem,
    CompletedItem,
    DraftAccumulator,
    FinishedTurn,
    HiddenItem,
    PageItem,
    ParagraphDraft,
    PublishedPage,
    RuntimeUpdate,
    StartedItem,
    TextDraft,
    TurnEnded,
    TurnStarted,
)
from agent_comms.tool_calls import ToolCall
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.tool_results import ToolDiff
from agent_comms.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    ThinkingTranscript,
    ToolEndTranscript,
    TranscriptEvent,
    UserTranscript,
)


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


class ThreadStatus(TaggedWire, ABC):
    """What Codex says its thread is doing; the bridge reports it on attaching."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True,
    )

    def update(self) -> RuntimeUpdate:
        return TurnEnded()


class NotLoadedStatus(ThreadStatus):
    wire_tag = 'notLoaded'


class IdleStatus(ThreadStatus):
    wire_tag = 'idle'


class SystemErrorStatus(ThreadStatus):
    wire_tag = 'systemError'


class ActiveStatus(ThreadStatus):
    wire_tag = 'active'

    def update(self) -> RuntimeUpdate:
        return TurnStarted()


class ResumedThreadState(WireModel):
    status: ThreadStatus.declared_union()
    cwd: str = ''


class ResumedThread(WireModel):
    thread: ResumedThreadState


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


if TYPE_CHECKING:
    from .codex import CodexConnection


class RpcReply(WireModel, ABC):
    @abstractmethod
    def route(self, connection: CodexConnection) -> None: ...


class RpcSuccess(RpcReply):
    id: int
    result: JsonValue

    def route(self, connection: CodexConnection) -> None:
        connection.answer(self.id).set_result(self.result)


class RpcFailure(RpcReply):
    id: int | None
    error: RpcError

    def route(self, connection: CodexConnection) -> None:
        connection.answer(self.id).set_exception(RuntimeError(f'Codex: {self.error.message}'))


class RpcNotification(RpcReply):
    method: str
    params: JsonValue

    def route(self, connection: CodexConnection) -> None:
        connection.pending.append(self)


RPC_RESPONSE = TypeAdapter(RpcSuccess | RpcFailure | RpcNotification)


class Item(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='__name__',
        skip_if_no_key=True,
    )
    id: str
    # What the CLI is doing while it produces this item.
    activity: ClassVar[ActivityState] = ActivityState.WORKING
    doing: ClassVar[str] = ''

    @abstractmethod
    def events(self) -> tuple[TranscriptEvent, ...]:
        """The finished item's presentation."""

    def completed(self, turn_id: str, events: tuple[TranscriptEvent, ...]) -> RuntimeUpdate:
        return CompletedItem(turn_id, self.id, events)

    def accumulator(self, started_at: EventTime) -> DraftAccumulator:
        return DraftAccumulator(started_at)

    @property
    def activity_detail(self) -> str:
        return self.doing

class TextBody(WireModel):
    text: str

    def accumulator(self, started_at: EventTime) -> TextDraft:
        return TextDraft.from_parts(AssistantTranscript, (self.text,), started_at, self.id)

    def events(self) -> tuple[TranscriptEvent, ...]:
        return self.accumulator(UnrecordedEventTime()).events


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

    def accumulator(self, started_at: EventTime) -> ParagraphDraft:
        # Only public summaries; private/raw reasoning is never projected.
        return ParagraphDraft.from_parts(ThinkingTranscript, self.summary, started_at, self.id)

    def events(self) -> tuple[TranscriptEvent, ...]:
        return self.accumulator(UnrecordedEventTime()).events


class UserItem(Item):
    wire_tag = 'userMessage'
    content: list[CodexUserInput]
    clientId: str | None = None

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (UserTranscript('\n'.join(part.display for part in self.content), position=self.id),)

    def completed(self, turn_id: str, events: tuple[TranscriptEvent, ...]) -> RuntimeUpdate:
        # Bus inputs already appear in Core's inbox; the live view does not echo them as new input.
        if BusInput.identifies(self.clientId):
            return HiddenItem(events)
        return super().completed(turn_id, events)


class ToolItem(Item, ABC):
    @property
    @abstractmethod
    def tool_name(self) -> str: ...

    @property
    @abstractmethod
    def arguments(self) -> dict[str, JsonValue]: ...

    @property
    @abstractmethod
    def output(self) -> str: ...

    @property
    @abstractmethod
    def succeeded(self) -> bool: ...

    @property
    def call(self) -> ToolCall:
        shown = ToolCall(self.id, self.tool_name, self.arguments)
        tool = ToolDeclaration.record(self.tool_name, self.arguments, call_id=self.id)
        return shown.recorded(tool) if tool else shown

    def events(self) -> tuple[TranscriptEvent, ...]:
        return self.call.finished(self.output, self.succeeded, self.id)

    @property
    def activity_detail(self) -> str:
        return self.call.title


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
    def arguments(self) -> dict[str, JsonValue]:
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
    input_value: dict[str, JsonValue] = Field(alias='arguments')
    contentItems: list[ToolContent] | None = None
    success: bool | None = None

    @property
    def tool_name(self) -> str:
        return self.tool

    @property
    def arguments(self) -> dict[str, JsonValue]:
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
    input_value: dict[str, JsonValue] = Field(alias='arguments')
    status: str
    result: McpResult | None = None
    error: McpError | None = None

    @property
    def tool_name(self) -> str:
        return f'{self.server}/{self.tool}'

    @property
    def arguments(self) -> dict[str, JsonValue]:
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


class FunctionOutputItem(Item):
    wire_tag = 'functionCallOutput'
    name: str
    output: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (ToolEndTranscript(position=self.id, tool_call_id=self.id, tool_name=self.name, text=self.output),)


class SendOutputItem(FunctionOutputItem):
    name: Literal[SEND_TOOL]

    def events(self) -> tuple[TranscriptEvent, ...]:
        # The bridge's send result is already rendered from the bus in Toad.
        return ()


class CompactionItem(Item):
    wire_tag = 'contextCompaction'
    doing = 'Compacting context'

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript('Context compacted.', position=self.id),)


class HookPromptItem(Item):
    wire_tag = 'hookPrompt'

    def events(self) -> tuple[TranscriptEvent, ...]:
        # Harness instructions are not assistant output.
        return ()


class ReviewBody(WireModel):
    review: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(self.review, position=self.id),)


class EnteredReviewItem(ReviewBody, Item):
    wire_tag = 'enteredReviewMode'


class ExitedReviewItem(ReviewBody, Item):
    wire_tag = 'exitedReviewMode'


class SleepItem(Item):
    wire_tag = 'sleep'
    durationMs: int

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Waited ({self.durationMs} ms requested).', position=self.id),)

    @property
    def activity_detail(self) -> str:
        return f'Waiting {self.durationMs} ms'


class ImageViewItem(Item):
    wire_tag = 'imageView'
    path: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Image: {self.path}', position=self.id),)


class ImageGenerationItem(Item):
    wire_tag = 'imageGeneration'
    status: str
    result: str
    savedPath: str | None = None

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'Image generation: {self.status}' +
                                 (f' — {self.savedPath}' if self.savedPath else ''), position=self.id),)


class WebSearchItem(Item):
    wire_tag = 'webSearch'
    query: str
    action: JsonValue = None
    results: JsonValue = None

    def events(self) -> tuple[TranscriptEvent, ...]:
        # Search status carries no success flag. Do not invent a successful end.
        return (NoticeTranscript(f'Web search: {self.query}', position=self.id),)

    @property
    def activity_detail(self) -> str:
        return f'Searching: {self.query}'


class PatchChange(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    @abstractmethod
    def patch(self, path: str, content: str) -> ToolDiff | None: ...


class AddedFile(PatchChange):
    wire_tag = 'add'

    def patch(self, path: str, content: str) -> ToolDiff | None:
        return ToolDiff.from_versions(path, '', content, before_path='/dev/null')


class DeletedFile(PatchChange):
    wire_tag = 'delete'

    def patch(self, path: str, content: str) -> ToolDiff | None:
        return ToolDiff.from_versions(path, content, '', after_path='/dev/null')


class UpdatedFile(PatchChange):
    wire_tag = 'update'
    move_path: str | None = None

    def patch(self, path: str, content: str) -> ToolDiff:
        before, after = path, path if self.move_path is None else self.move_path
        if self.move_path is not None:
            # Codex appends the destination outside its unified hunk text.
            content = content.removesuffix(f'\n\nMoved to: {self.move_path}')
        return ToolDiff(f'--- {before}\n+++ {after}\n{content}')


class FileChange(WireModel):
    path: str
    diff: str
    kind: PatchChange.declared_union()

    def events(self, call_id: str, ok: bool) -> tuple[TranscriptEvent, ...]:
        call = ToolCall(call_id, 'edit', {'path': self.path}).recorded(FilePatchTool(path=self.path))
        diff = self.kind.patch(self.path, self.diff)
        return (call.patched(diff) if diff else call).finished('', ok, call_id)


class FileChangeItem(Item):
    wire_tag = 'fileChange'
    doing = 'Editing files'
    changes: list[FileChange]
    status: Literal['inProgress', 'completed', 'failed', 'declined']

    def events(self) -> tuple[TranscriptEvent, ...]:
        if self.status == 'inProgress':
            raise ValueError('File change completion still says inProgress')
        return tuple(event for index, change in enumerate(self.changes)
                     for event in change.events(f'{self.id}:{index}', self.status == 'completed'))


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
            for name, state in self.agentsStates.items()), position=self.id),)


class AgentActivityItem(Item):
    wire_tag = 'subAgentActivity'
    agentPath: str
    agentThreadId: str
    kind: str

    def events(self) -> tuple[TranscriptEvent, ...]:
        return (NoticeTranscript(f'{self.agentPath}: {self.kind}', position=self.id),)


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
    completedAtMs: int | None = None

    @staticmethod
    def _decode_start(milliseconds: int | None) -> EventTime:
        return (
            UnrecordedEventTime() if milliseconds is None
            else EventTime.capture(milliseconds / 1000)
        )

    started_at: Annotated[EventTime, ValidateAs(int | None, _decode_start)] = Field(
        alias='startedAtMs', default_factory=UnrecordedEventTime,
    )

    @property
    def completed(self) -> bool:
        return self.completedAtMs is not None

    def publication(self) -> RuntimeUpdate:
        if self.completedAtMs is None:
            raise ValueError(f'Item {self.item.id} has not finished')
        event_time = self.started_at.continued_from(self.completedAtMs / 1000)
        return self.item.completed(self.turnId, tuple(
            event_time.stamp(event) for event in self.item.events()))


class ItemsPage(WireModel):
    data: list[ItemEntry]
    nextCursor: str | None = None

    def publication(self) -> PublishedPage:
        # Codex's nullable wire value is decoded here; no reader interprets it.
        match self.nextCursor:
            case str() as cursor:
                earlier = EarlierPage(cursor)
            case None:
                earlier = NoEarlierPage()
        return PublishedPage(tuple(PageItem(entry.turnId, entry.item.id, entry.publication())
                                   for entry in reversed(self.data) if entry.completed), earlier)


class TranscriptNotification(WireModel, ABC, metaclass=RegisteredWireMeta):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='method', skip_if_no_key=True)
    method: ClassVar[str]
    threadId: str

    @abstractmethod
    def apply(self, stream: ItemStream) -> RuntimeUpdate: ...


class TurnHeader(WireModel):
    id: str


class TurnBegan(TranscriptNotification):
    method = 'turn/started'
    turn: TurnHeader

    def apply(self, stream: ItemStream) -> RuntimeUpdate:
        return TurnStarted()


class TurnFinished(TranscriptNotification):
    method = 'turn/completed'
    turn: TurnHeader

    def apply(self, stream: ItemStream) -> RuntimeUpdate:
        return FinishedTurn(self.turn.id)


class ItemNotification(TranscriptNotification, ABC):
    turnId: str


class Started(ItemNotification):
    method = 'item/started'
    item: CodexItem
    startedAtMs: int

    def apply(self, stream: ItemStream) -> RuntimeUpdate:
        return StartedItem(self.turnId, self.item.id, self.item.activity, self.item.activity_detail,
                           self.item.accumulator(EventTime.capture(self.startedAtMs / 1000)))


class Completed(ItemNotification):
    method = 'item/completed'
    item: CodexItem
    completedAtMs: int

    def apply(self, stream: ItemStream) -> RuntimeUpdate:
        # Completion is authoritative, including after joining mid-item.
        return ItemEntry(turnId=self.turnId, item=self.item,
                         completedAtMs=self.completedAtMs).publication()


class TextDelta(ItemNotification, ABC):
    itemId: str
    delta: str
    def section(self) -> int:
        return 0

    def apply(self, stream: ItemStream) -> AppendDraftItem:
        return AppendDraftItem(self.turnId, self.itemId, self.delta, self.section())


class AssistantDelta(TextDelta):
    method = 'item/agentMessage/delta'


class ReasoningDelta(TextDelta):
    method = 'item/reasoning/summaryTextDelta'
    summaryIndex: int = Field(ge=0)

    def section(self) -> int:
        return self.summaryIndex


class ItemStream:
    """Decode this thread's parts; the shared output owns their accumulation."""

    def __init__(self, thread: str):
        self.thread = thread

    def receive(self, notification: RpcNotification) -> tuple[RuntimeUpdate, ...]:
        declarations = TranscriptNotification.__registry_config__.registry_dict
        if notification.method not in declarations:
            # Usage, account, raw/private reasoning and other control
            # events are not transcript content. Known methods declare a type.
            return ()
        event = declarations[notification.method].model_validate(notification.params)
        if event.threadId != self.thread:
            return ()
        return (event.apply(self),)
