"""CLI commands own their options, boundary decoding and operation bodies.

The parser is a projection of dataclass fields; DeclaredFamily is the sole
command catalog. Existing CLI spellings belong to the store_files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field, fields, replace
from itertools import dropwhile, takewhile
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, TextIO

from acp.exceptions import RequestError

from .active_route import ActiveRoute, replace_unreadable_active_route
from .activity import ActivityState
from .catalog_document import CatalogDocument
from .channel_management import KeepThreadsTagDisposition, TagChangeResult, TagDisposition
from .channel_targets import ChannelSelection, CommandTarget, NoChannel, OneChannel, ResolvedChannel
from .channels import ANY_CHANNEL, Channel
from .check_root import CheckRoot
from .child_process import NoProcess, ProcessIdentity, ProcessState
from .cli_bridges.claude import ClaudeTranscript
from .cli_bridges.history import History, OfflineHistory, OnlineHistory
from .cli_bridges.shared import ChoiceTargetField, CliBridge, DeclaredOptions, Send, TargetField
from .cli_protocol import ArrivedMessage, EarlierItems
from .cli_result import CliResult
from .command import Command
from .comms import Comms
from .config_options import (
    ConfigOptions,
    ModelConfigOption,
    ThinkingLevelConfigOption,
)
from .declared_family import DeclaredFamily
from .errors import UnregisteredThreadError
from .exporting import (
    ChannelScope,
    DmScope,
    EverythingScope,
    FullLimit,
    JsonlFormat,
    MaxBytesLimit,
    RecentLimit,
    SelectableWireExportFormat,
)
from .field_codec import FieldCodec, FieldPlan, PathText, WireForm, option
from .harness_install import Harness, HarnessInstalled
from .history_result import HistoryPageResult
from .history_views import CallingThreadTarget, NamedThreadTarget, PollTarget
from .importing import ImportedSessionMetadata, ImportLimits, ThreadImported
from .maintenance_barrier import MaintenanceHeldError
from .message_page import PageBounds
from .message_reference import MessageReference
from .messages import (
    Message,
)
from .native_settings import NativeThreadDefaults
from .native_turn_context import NativeContextData
from .owner_lifecycle import OwnerRestartResult, OwnerStartResult
from .page_traversal import OlderTraversal, PageCursor
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .presentation import CountedRead, InboxRead, ThreadRecord, ThreadView, UncountedRead
from .read_basis import OnReadWatchAcknowledgment, WatchAcknowledgment
from .registry_document import RegistrySnapshot
from .repo import Repo
from .runtime import RuntimeConnection, socket_path
from .schema_report import StoreSchemaReport, StoreSchemaReports
from .send_result import SendResult
from .shared_ledger import LedgerDocument
from .thread_execution import (
    ArchiveOwnerControl,
    ExternalThreadExecution,
    NativeOwnerCommand,
    OwnerLifecycleControl,
    StartOwnerControl,
    ThreadCommandAvailability,
    ThreadExecution,
)
from .thread_management import ForkSpec, NewThreadSpec, ThreadDeleted
from .thread_status import ThreadStatus
from .threads import Thread
from .tool_result import ToolResult
from .turn_context import (
    CodexRolloutProvenance,
    ContextManifest,
    ContextManifestChange,
    ContextSegment,
    ContributionCoordinates,
    NextContextTurn,
    Provenance,
    TurnContext,
)
from .wire_log import WireLog
from .working_memory_labels import ClassifierVersion, ModelLabel, QuestionVersion
from .working_memory_questions import SpanAnswer


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d|w)", value.strip().lower())
    if match is None:
        raise argparse.ArgumentTypeError("duration must look like 30s, 15m, 24h, 7d, or 2w")
    amount = float(match.group(1))
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


def _tags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def _tag_text(value: frozenset[str] | None) -> str | None:
    return None if value is None else ', '.join(sorted(value))


def _shell_words(value: str | None) -> list[str] | None:
    return shlex.split(value) if value is not None else None


def _message_reference(value: str) -> dict[str, object]:
    """CLI spelling decodes once into a wire reference."""
    sequence, message_id = value.split(":", 1)
    if int(sequence) <= 0 or not message_id:
        raise ValueError("Source must be a committed sequence:message_id reference")
    return {"seq": int(sequence), "message_id": message_id}


@dataclass(frozen=True)
class TargetAction:
    """A selection keeps the bound commands, not a separate permission list."""
    bound: tuple[CliCommand, ...]
    targets: tuple[str, ...]
    channels: ChannelSelection = NoChannel()

    @property
    def declaration(self) -> type[CliCommand]:
        member = type(self.bound[0])
        return member.catalog_declaration() if len(self.targets) > 1 else member

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        return self.bound[0].editable_fields

    async def prepare_fields(self) -> tuple[TargetField, ...]:
        return await self.bound[0].prepare_fields()

    @property
    def label(self) -> str:
        return self.declaration.help

    @property
    def confirmation(self) -> str:
        groups: dict[type[CliCommand], list[CliCommand]] = {}
        for command in self.bound:
            groups.setdefault(type(command), []).append(command)
        return '\n'.join(filter(None, (commands[0].selection_confirmation(tuple(commands))
                                       for commands in groups.values())))

    def edited(self, arguments: dict[str, str | None]) -> TargetAction:
        return replace(self, bound=tuple(command.edited(type(command).editor_arguments(arguments))
                                        for command in self.bound))

    def with_confirmation(self, confirmed: bool) -> TargetAction:
        return replace(self, bound=tuple(command.with_confirmation(confirmed)
                                        for command in self.bound))

    def encode(self) -> dict[str, object]:
        """Only the CLI boundary requests the external catalog JSON shape."""
        return {'command': FieldCodec.encode(self.declaration), 'label': self.label,
                'parameters': {'type': 'object', 'additionalProperties': False,
                    'properties': {item.name: item.json_schema() for item in self.editable_fields},
                    'required': [item.name for item in self.editable_fields if item.required]},
                'confirmation': self.confirmation, 'targets': list(self.targets),
                'bound_count': len(self.bound)}


class TargetOutcome:
    """One attempted selection member; failure never claims absence of effects."""
    successful: ClassVar[bool] = False

    def reconnect_targets(self) -> tuple[str, ...]:
        return ()

    def open_targets(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class TargetCompleted(TargetOutcome):
    target: str
    command: CliCommand
    result: Annotated[CliResult, WireForm]
    successful: ClassVar[bool] = True

    def reconnect_targets(self) -> tuple[str, ...]:
        return self.result.reconnect_targets()

    def open_targets(self) -> tuple[str, ...]:
        return self.result.open_targets()


@dataclass(frozen=True)
class TargetFailed(TargetOutcome):
    target: str
    error_type: str
    error: str


@dataclass(frozen=True)
class TargetBatchResult(CliResult):
    outcomes: tuple[TargetCompleted | TargetFailed, ...]

    @property
    def successful(self) -> bool:
        return all(outcome.successful for outcome in self.outcomes)

    def reconnect_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for outcome in self.outcomes
                                   for name in outcome.reconnect_targets()))

    def open_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for outcome in self.outcomes
                                   for name in outcome.open_targets()))

    def exit_code(self) -> int:
        return int(not self.successful)


class CommandSelectionChanged(ValueError):
    """The selected command is no longer bound to the captured target."""


@dataclass(frozen=True)
class TargetSelection(ABC):
    """The targets one execution runs on; each kind owns how its result comes back."""
    targets: tuple[CommandTarget, ...]

    @staticmethod
    def decode(raw: CommandTarget | str | tuple[CommandTarget | str, ...], *, multiple: bool) -> TargetSelection:
        if isinstance(raw, tuple):
            if not raw:
                raise ValueError('Select at least one target')
            names = tuple(dict.fromkeys(CommandTarget.named(item) for item in raw))
            return (OneTarget if len(names) == 1 and not multiple else TargetBatch)(names)
        return OneTarget((CommandTarget.named(raw),))

    @abstractmethod
    def run(self, declaration: type[CliCommand], comms: Comms, arguments: dict[str, object],
            *, confirmed: bool, channel: ChannelSelection, project: str,
            snapshot: RegistrySnapshot, catalog: CatalogDocument | None) -> CliResult:
        """Bind, authorize and apply the declaration on this selection."""


class OneTarget(TargetSelection):
    """A single target: resolve errors raise and the bound command's own result returns."""

    def run(self, declaration, comms, arguments, *, confirmed, channel: ChannelSelection, project, snapshot, catalog):
        name, = self.targets
        resolved = name.resolve(snapshot, comms.bus.require_channel)
        bindings = tuple(bound for context in name.contexts(declaration, channel)
                         for bound in declaration.bindings(comms, name, context, snapshot=snapshot,
                                                           catalog=catalog, resolved=resolved))
        if len(bindings) == 1:
            bound = bindings[0].for_editor(comms, name.name, project).edited(arguments)
            return bound.with_confirmation(confirmed).apply(comms)
        if not bindings:
            raise ValueError('This action is no longer available for the target')
        # Several bindings on one target run as a batch of them.
        return TargetBatch(self.targets).run(declaration, comms, arguments, confirmed=confirmed,
                                             channel=channel, project=project,
                                             snapshot=snapshot, catalog=catalog)


class TargetBatch(TargetSelection):
    """Every other selection: each target's failure is collected and a TargetBatchResult returns."""

    def run(self, declaration, comms, arguments, *, confirmed, channel: ChannelSelection, project, snapshot, catalog):
        if not declaration.multiple_targets:
            raise ValueError('This action requires a single target')
        declarations = (tuple(member for member in CliCommand.members_with(CliCommand)
                              if member.catalog_declaration() is declaration)
                        if declaration.catalog_declaration() is declaration else (declaration,))
        outcomes: list[tuple[int, TargetCompleted | TargetFailed]] = []
        planned: dict[CliCommand, tuple[int, CommandTarget, str | None]] = {}
        for name in self.targets:
            try:
                resolved = name.resolve(snapshot, comms.bus.require_channel)
            except UnregisteredThreadError as error:
                outcomes.append((len(planned) + len(outcomes),
                                 TargetFailed(name.name, type(error).__name__, str(error))))
                continue
            for context in name.contexts(declaration, channel):
                try:
                    bindings = tuple(command for member in declarations
                                     for command in member.bindings(comms, name, context,
                                                                   snapshot=snapshot, catalog=catalog, resolved=resolved))
                    if not bindings:
                        scope = f' in {context}' if context is not None else ''
                        raise CommandSelectionChanged(f'This action is no longer available for {name}{scope}')
                except (CommandSelectionChanged, UnregisteredThreadError) as error:
                    outcomes.append((len(planned) + len(outcomes),
                                     TargetFailed(name.name, type(error).__name__, str(error))))
                    continue
                for command in bindings:
                    planned.setdefault(command, (len(planned) + len(outcomes), name, context))
        # Parameters and every existing warning are authorized before any write.
        edited = tuple(command.for_editor(comms, name.name, project).edited(arguments).with_confirmation(confirmed)
                       for command, (_, name, _) in planned.items())
        for (bound_command, (position, name, context)), command in zip(planned.items(), edited, strict=True):
            try:
                current = comms.registry.snapshot()
                current_catalog = (comms.channels.catalog.read()
                                   if context or name.needs_channel_state else None)
                if bound_command not in type(bound_command).bindings(
                        comms, name, context, snapshot=current, catalog=current_catalog,
                        resolved=name.resolve(current, comms.bus.require_channel)):
                    scope = f' in {context}' if context is not None else ''
                    raise CommandSelectionChanged(f'The bound command for {name}{scope} changed; refresh the selection')
                result = TargetCompleted(name.name, command, command.apply(comms))
            except (CommandSelectionChanged, UnregisteredThreadError) as error:
                result = TargetFailed(name.name, type(error).__name__, str(error))
            outcomes.append((position, result))
        return TargetBatchResult(tuple(outcome for _, outcome in sorted(outcomes, key=lambda item: item[0])))


@dataclass(frozen=True)
class TargetEdit(Command):
    """Accepted editor values use the declared field and current bound command."""
    declaration: type[CliCommand]
    target: TargetSelection
    arguments: dict[str, str | None]
    confirmed: bool = False
    channel: ChannelSelection = NoChannel()
    project: str | None = None

    def apply(self, ctx: Comms) -> CliResult:
        arguments = self.declaration.editor_arguments(self.arguments)
        return self.declaration.execute_target(ctx, self.target, arguments,
                                              confirmed=self.confirmed, channel=self.channel, project=self.project)


@dataclass(frozen=True)
class ThreadStoppedResult(CliResult, ToolResult):
    stopped: str


@dataclass(frozen=True)
class ThreadArchivedResult(CliResult, ToolResult):
    archived: str


@dataclass(frozen=True)
class ThreadCreatedResult(CliResult, ToolResult):
    created: str
    process_identity: ProcessState

    def open_targets(self) -> tuple[str, ...]:
        return (self.created,)


@dataclass(frozen=True)
class ThreadTagsResult(CliResult):
    thread: str
    tags: frozenset[str]


@dataclass(frozen=True)
class TagRenamedResult(CliResult):
    renamed: str
    name: str


@dataclass(frozen=True)
class ViewDeletedResult(CliResult):
    deleted_view: str


@dataclass(frozen=True)
class TargetReadResult(CliResult):
    read: str


@dataclass(frozen=True)
class ThreadPinnedResult(CliResult):
    thread: str
    pinned: bool


@dataclass(frozen=True)
class TargetCatalogResult(CliResult):
    actions: tuple[TargetAction, ...]

    def to_wire(self) -> object:
        return {'actions': [action.encode() for action in self.actions]}


@dataclass(frozen=True)
class MessageSentResult(CliResult):
    sent: SendResult

    def to_wire(self) -> object:
        message_id = self.sent.message.message_id
        return {'id': message_id, 'message': f'Message {message_id} was sent',
                'correction': FieldCodec.encode(self.sent.correction),
                'notices': self.sent.notices}


@dataclass(frozen=True)
class UserMessageSentResult(CliResult):
    sent: MessageReference


@dataclass(frozen=True)
class ThreadConfiguredResult(CliResult):
    configured: str
    model: str | None
    thinking_level: ThinkingLevelField
    service_tier: ServiceTier


@dataclass(frozen=True)
class ConstraintPinnedResult(CliResult):
    pin: MessageReference
    source: MessageReference


@dataclass(frozen=True)
class ConstraintSupersededResult(CliResult):
    supersession: MessageReference


@dataclass(frozen=True)
class ConstraintDroppedResult(CliResult):
    drop: MessageReference


@dataclass(frozen=True)
class InboxResult(CliResult):
    messages: tuple[Message, ...]

    def to_wire(self) -> object:
        return {"messages": [message.to_wire() for message in self.messages]}


@dataclass(frozen=True)
class AcknowledgedResult(CliResult):
    acknowledged: int


@dataclass(frozen=True)
class WhoResult(CliResult):
    who: tuple[Annotated[ThreadView, WireForm], ...]


@dataclass(frozen=True)
class ThreadActivityRow:
    thread: str
    state: ActivityState
    detail: str
    ts: float


@dataclass(frozen=True)
class ActiveStatusResult(CliResult):
    status: tuple[ThreadActivityRow, ...]


@dataclass(frozen=True)
class ChannelsResult(CliResult):
    channels: tuple[str, ...]


@dataclass(frozen=True)
class ThreadsResult(CliResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]


@dataclass(frozen=True)
class RegisteredResult(CliResult):
    registered: str


@dataclass(frozen=True)
class HeartbeatResult(CliResult):
    heartbeat: str


@dataclass(frozen=True)
class SessionAttachedResult(CliResult):
    attached: str
    session_file: str | None
    pid: int | None = field(default=None, metadata={"wire_omit_default": True})


@dataclass(frozen=True)
class CheckpointResult(CliResult):
    thread: str
    checkpoint: str
    branch: str
    reason: str | None = field(default=None, metadata={"wire_omit_default": True})
    commit: str | None = field(default=None, metadata={"wire_omit_default": True})


@dataclass(frozen=True)
class ActivitySetResult(CliResult):
    activity: str
    state: str


@dataclass(frozen=True)
class OwnersRestartedResult(CliResult):
    restarted: tuple[OwnerRestartResult, ...]


@dataclass(frozen=True)
class OwnerReleasedResult(CliResult):
    released: str


@dataclass(frozen=True)
class LedgerMergedResult(CliResult):
    merged: bool = True


@dataclass(frozen=True)
class SubagentsImportedResult(CliResult):
    imported: tuple[ThreadImported, ...]


@dataclass(frozen=True)
class InstallResult(CliResult):
    config: str
    harnesses: Mapping[str, HarnessInstalled]

    def to_wire(self) -> object:
        # Each harness's installation sits beside the config path on the wire.
        return {"config": self.config,
                **{name: FieldCodec.encode(installed)
                   for name, installed in self.harnesses.items()}}


@dataclass(frozen=True)
class ImportedContextSources(CliResult):
    scope: str
    sources: tuple[CodexRolloutProvenance, ...]


@dataclass(frozen=True)
class RecordedContextManifests(CliResult):
    manifests: tuple[ContextManifest, ...]
    text_recorded: bool


@dataclass(frozen=True)
class CoreSegmentText:
    kind: type[ContextSegment]
    provenance: tuple[Provenance, ...]
    text: str


@dataclass(frozen=True)
class CoreContextInspection(CliResult):
    """Current core contributors; native_error says why the native base is absent."""
    scope: str
    input_supplied: bool
    core_context: TurnContext
    segments: tuple[CoreSegmentText, ...]
    native_error: str


@dataclass(frozen=True)
class NativeSourceSpans:
    segment: int
    sha256: str
    spans: tuple[ContributionCoordinates, ...]


@dataclass(frozen=True)
class NativeContextInspection(CliResult):
    """The next native base together with the current core contributors."""
    scope: str
    input_supplied: bool
    core_context: TurnContext
    segments: tuple[CoreSegmentText, ...]
    native_manifest: tuple[ContextSegment, ...]
    native_provider_context: Mapping[str, object]
    native_source_spans: tuple[NativeSourceSpans, ...]


@dataclass(frozen=True, kw_only=True)
class CliCommand(
    DeclaredFamily, DeclaredOptions, Command, affix="CliCommand", discovery_package="agent_comms"
):
    multiple_targets: ClassVar[bool] = False

    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        """Concrete thread/channel variants declare their shared menu operation."""
        return cls

    @classmethod
    def selected_targets(cls, raw: CommandTarget | str | tuple[CommandTarget | str, ...]) -> TargetSelection:
        """Decode a CLI or menu target input once, before command execution."""
        return TargetSelection.decode(raw, multiple=cls.multiple_targets)

    @classmethod
    def channel_fields(cls) -> tuple[FieldPlan, ...]:
        """The declared target-bound channel parameters own membership scope."""
        return tuple(entry for entry in FieldCodec.argument_fields(cls)
                     if entry.key == 'channel' and entry.setting('target_bound', False))

    @classmethod
    def selection_channels(cls, channel: ChannelSelection,
                           target: str) -> tuple[str | None, ...]:
        return channel.channels_for(target)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None) -> tuple[Self, ...]:
        return ()

    @classmethod
    def project_bindings(cls, comms, project: str) -> tuple[Self, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog) -> tuple[Self, ...]:
        return ()

    @classmethod
    def bindings(cls, comms: Comms, target: CommandTarget | str, channel: str | None = None,
                 *, snapshot: RegistrySnapshot, catalog: CatalogDocument | None,
                 resolved) -> tuple[Self, ...]:
        return CommandTarget.named(target).bindings(
            cls, comms, channel, resolved, snapshot=snapshot, catalog=catalog)

    @classmethod
    def target_catalog(cls, comms: Comms, target: TargetSelection,
                       channel: ChannelSelection = NoChannel(),
                       *, project: str) -> tuple[TargetAction, ...]:
        selected = target.targets
        # A catalog is one observation. Declarations borrow its original store
        # resources; execution still acquires and rebinds current state.
        snapshot = comms.registry.snapshot()
        # View actors (including an implicit project actor) need not declare a
        # thread. Discovery has no thread actions for absent members; execution
        # still requires its original current binding. Resolve aliases through
        # the acquired namespace rather than reserving a special actor spelling.
        available_targets = tuple(item for item in selected if item.available(snapshot))
        catalog = (comms.channels.catalog.read()
                   if channel.needs_catalog or any(item.needs_channel_state for item in selected) else None)
        resolved_targets = tuple((name, name.resolve(snapshot, comms.bus.require_channel)) for name in available_targets)
        grouped: dict[type[CliCommand], dict[CliCommand, None]] = {}
        for member in cls.members_with(cls):
            if len(selected) > 1 and not member.multiple_targets:
                continue
            selected_channels = channel if member.channel_fields() else NoChannel()
            for name, resolved in resolved_targets:
                for context in name.contexts(member, selected_channels):
                    available = member.bindings(
                        comms, name, context, snapshot=snapshot, catalog=catalog, resolved=resolved)
                    if available:
                        declaration = member.catalog_declaration() if len(selected) > 1 else member
                        grouped.setdefault(declaration, {}).update(dict.fromkeys(
                            command.for_editor(comms, name.name, project) for command in available))
        names = tuple(name for item in selected for name in item.names)
        return tuple(TargetAction(tuple(commands), names,
                                  channel if declaration.channel_fields() else NoChannel())
                     for declaration, commands in grouped.items())

    def for_editor(self, comms: Comms, target: str, project: str) -> Self:
        return self

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        """The command owns the parameters offered by its target action."""
        return tuple(TargetField.of(entry, entry.current(self))
                     for entry in FieldCodec.argument_fields(type(self))
                     if not entry.setting('target_bound', False))

    async def prepare_fields(self) -> tuple[TargetField, ...]:
        catalogs = ConfigOptions.configured()
        return tuple([await item.prepare(catalogs, self) for item in self.editable_fields])

    def describe(self, comms: Comms, target: CommandTarget | str, project: str) -> TargetAction:
        target = CommandTarget.named(target)
        bound = self.for_editor(comms, target.name, project)
        channels = next((OneChannel(getattr(bound, item.name))
                         for item in bound.channel_fields()), NoChannel())
        return TargetAction((bound,), target.names, channels)

    def run(self, ctx: Comms, output: TextIO) -> int:
        """Run this command and write its declared result."""
        result: CliResult = self.apply(ctx)
        json.dump(result.to_wire(), output, indent=2)
        output.write("\n")
        return result.exit_code()

    @classmethod
    def editor_arguments(cls, values: dict[str, str | None]) -> dict[str, object]:
        return FieldCodec.editor_arguments(cls, values)

    def confirmation(self) -> str:
        return ''

    def selection_confirmation(self, commands: tuple[CliCommand, ...]) -> str:
        return '\n'.join(dict.fromkeys(warning for command in commands
                                      if (warning := command.confirmation())))

    def with_confirmation(self, confirmed: bool) -> Self:
        if self.confirmation() and not confirmed:
            raise ValueError(self.confirmation())
        return self

    def edited(self, arguments: dict[str, object]) -> Self:
        bound = {entry.key for entry in FieldCodec.argument_fields(type(self))
                 if entry.setting('target_bound', False)}
        if bound & arguments.keys():
            raise ValueError('Target-bound parameters cannot be overridden')
        editable = {item.name for item in self.editable_fields}
        if arguments.keys() - editable:
            raise ValueError('Parameters not offered by this target command cannot be overridden')
        encoded = FieldCodec.encode(self)
        return type(self).from_payload({**{key: encoded[key] for key in bound & encoded.keys()},
                                       'kind': FieldCodec.encode(type(self)), **arguments})

    @classmethod
    def execute_target(cls, comms: Comms, target: TargetSelection, arguments: dict[str, object],
                       *, confirmed: bool = False,
                       channel: ChannelSelection = NoChannel(),
                       project: str | None = None) -> CliResult:
        if not cls.channel_fields():
            channel.require_unbound()
            channel = NoChannel()
        snapshot = comms.registry.snapshot()
        catalog = (comms.channels.catalog.read()
                   if channel.needs_catalog or any(item.needs_channel_state for item in target.targets) else None)
        return target.run(cls, comms, arguments, confirmed=confirmed, channel=channel,
                             project=os.getcwd() if project is None else project,
                             snapshot=snapshot, catalog=catalog)


@dataclass(frozen=True, kw_only=True)
class CheckRootCliCommand(CliCommand, declared_name="check-root"):
    help = "Run a command against selected recent history, then remove its private root"
    threads: tuple[str, ...] = option(normalize=_tags, help="Comma-separated source thread names")
    into: Annotated[Path, PathText] = option(
        help="New directory under your agent scratch directory"
    )
    argv: tuple[str, ...] = option("argv", nargs=argparse.REMAINDER,
                                 help="-- COMMAND ARGS; use {root} for the private root argument")

    def apply(self, ctx: Comms) -> CliResult:
        return CheckRoot(ctx, self.threads, self.into).run(self.argv)


@dataclass(frozen=True, kw_only=True)
class ImportingCliCommand(CliCommand):
    """Where an imported thread works and how much of its source conversation it keeps."""
    worktree: str | None = option(
        "--worktree", help="Override the source project directory", default=None
    )
    model: str | None = option(
        "--model",
        help="Target Pi provider/model; not inferred from foreign model IDs",
        default=None,
    )
    tags: frozenset[str] = option(
        "--tags", default_factory=frozenset, parser_default="", normalize=_tags
    )
    max_messages: int = option("--max-messages", default=200)
    max_characters: int = option("--max-characters", default=120000)
    max_message_characters: int = option("--max-message-characters", default=12000)

    def imported(self, ctx: Comms, history: History, name: str) -> ThreadImported:
        return ctx.threads.import_thread(
            history, name=name, worktree=self.worktree, model=self.model, tags=self.tags,
            limits=ImportLimits(self.max_messages, self.max_characters, self.max_message_characters),
        )


@dataclass(frozen=True, kw_only=True)
class ImportThreadCliCommand(ImportingCliCommand, declared_name="import-thread"):
    help = "Import a stored Claude Code, Codex or OpenCode session as a stopped thread"
    harness: type[Harness] = option("--harness", editor=ChoiceTargetField)
    source: Annotated[Path, PathText] = option(
        "--source", help="Claude Code or Codex session JSONL, OpenCode export JSON or opencode.db")
    session_id: str | None = option(
        "--session-id", help="Required for OpenCode database imports", default=None
    )
    name: str = option("--name", help="New agent-comms thread name")

    def apply(self, ctx: Comms) -> CliResult:
        return self.imported(ctx, OfflineHistory.of(
            self.harness, self.source.expanduser().resolve(), self.session_id), self.name)


@dataclass(frozen=True, kw_only=True)
class ImportRunningThreadCliCommand(ImportingCliCommand, declared_name="import-running-thread"):
    help = "Import a session a running Claude Code, Codex or OpenCode holds, as a stopped thread"
    harness: type[Harness] = option("--harness", editor=ChoiceTargetField)
    thread: str = option("--thread", help="The harness's own session or thread id")
    address: str | None = option(
        "--address", help="Codex app-server socket, or OpenCode server URL", default=None)
    name: str = option("--name", help="New agent-comms thread name")

    def apply(self, ctx: Comms) -> CliResult:
        directory = Path(self.worktree).expanduser() if self.worktree else Path.cwd()
        return self.imported(ctx, OnlineHistory.of(self.harness, self.thread, self.address, directory), self.name)


@dataclass(frozen=True, kw_only=True)
class ImportSubagentsCliCommand(ImportingCliCommand, declared_name="import-subagents"):
    help = "Import every subagent of a stored Claude Code session, each as a stopped thread"
    session: Annotated[Path, PathText] = option("--session", help="The Claude Code session JSONL")
    prefix: str = option("--prefix", default="", help="Prepended to each subagent id to name its thread")

    def apply(self, ctx: Comms) -> SubagentsImportedResult:
        return SubagentsImportedResult(tuple(
            self.imported(ctx, ClaudeTranscript(path), self.prefix + path.stem.removeprefix("agent-"))
            for path in ClaudeTranscript.subagents(self.session.expanduser().resolve())))


@dataclass(frozen=True, kw_only=True)
class SendCliCommand(Send, CliCommand):
    sender: str = option("--from")

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return False

    def apply(self, ctx: Comms) -> MessageSentResult:
        return MessageSentResult(self.report.send(ctx, self))


@dataclass(frozen=True, kw_only=True)
class UserSendCliCommand(CliCommand, declared_name="user-send"):
    help = "Send as the human user to a thread (DM), #channel, or #all"
    target: str = option("--to")
    body: str = option("--body")
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def apply(self, ctx: Comms) -> CliResult:

        message = ctx.messaging.send_user_message(self.target, self.body, worktree=self.worktree)
        return UserMessageSentResult(message.reference)


@dataclass(frozen=True, kw_only=True)
class ConfigureCliCommand(CliCommand, declared_name="configure"):
    help = "Set a stopped thread's model, thinking level, or service tier for its next start"
    name: str = option("--name")
    model: str | None = option("--model", default=None)
    thinking: str | None = option("--thinking", default=None)
    service_tier: ServiceTier | None = option("--service-tier", default=None)

    def apply(self, ctx: Comms) -> CliResult:
        from .errors import RelationViolationError

        thread = ctx.registry.require(self.name)
        if ctx.registry.status(thread.name).active:
            raise RelationViolationError(
                f"{thread.name} is running; change its settings through its session")
        level = FieldCodec.decode(ThinkingLevelField, self.thinking)
        candidate = thread.with_model_configuration(
            model=self.model,
            thinking_level=level,
            service_tier=self.service_tier,
        )
        asyncio.run(ConfigOptions.configured().validate(candidate))
        updated = ctx.threads.set_thread_configuration(
            thread,
            model=candidate.model,
            thinking_level=candidate.thinking_level,
            service_tier=candidate.service_tier,
        )
        return ThreadConfiguredResult(updated.name, updated.model, updated.thinking_level,
                                      updated.service_tier)


@dataclass(frozen=True, kw_only=True)
class PinConstraintCliCommand(CliCommand, declared_name="pin-constraint"):
    help = "Pin a checked original human message for its recipient"
    thread: str = option("thread")
    source: MessageReference = option("--source", normalize=_message_reference,
                                      help="Original sequence:message_id")
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def original_source(self, ctx: Comms):
        return self.source

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.pin_user_constraint(self.thread, source, worktree=self.worktree)

    def apply(self, ctx: Comms) -> CliResult:

        source = self.original_source(ctx)
        pin = self.pin_original(ctx, source)
        return ConstraintPinnedResult(pin.reference, source)


@dataclass(frozen=True, kw_only=True)
class PinInputConstraintCliCommand(PinConstraintCliCommand, declared_name="pin-input-constraint"):
    help = "Pin an original human input for its recipient"
    source: str = option("--source", help="Original durable input key")

    def original_source(self, ctx: Comms):
        from .input_disposition import InputDispositions

        return InputDispositions(ctx.root).lookup(self.source).context_provenance().require_human_input()

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.pin_input_constraint(self.thread, source, worktree=self.worktree)


@dataclass(frozen=True, kw_only=True)
class ApproveAnnotationsCliCommand(PinConstraintCliCommand, declared_name="approve-annotations"):
    help = "Approve a pinned external classifier using original human wording and an hourly request budget"
    per_hour: int = option("--per-hour", help="Maximum classifier requests in the rolling hour")
    segments: tuple[type[ContextSegment], ...] = option("--segments", type=json.loads,
        help="JSON list of original segment kinds permitted for disclosure")

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.approve_annotations(self.thread, source, worktree=self.worktree,
                                                per_hour=self.per_hour, segments=self.segments)


@dataclass(frozen=True, kw_only=True)
class CorrectAnnotationCliCommand(CliCommand, declared_name="correct-annotation"):
    help = "Correct one original stored model answer through the human identity owner"
    label: ModelLabel = option("--label", type=json.loads)
    answer: type[SpanAnswer] = option("--answer", editor=ChoiceTargetField)
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def apply(self, ctx: Comms) -> CliResult:
        from .coordinator import Coordination

        author = ctx.messaging.user_identity(self.worktree).incarnation
        snapshot = ctx.registry.snapshot()
        with Coordination(str(ctx.root / "coordination.sqlite3")) as store:
            return store.annotations.correct(self.label, self.answer, author, snapshot)


@dataclass(frozen=True, kw_only=True)
class AnnotationsCalibrationCliCommand(CliCommand, declared_name="annotations"):
    help = "Report human-reviewed accuracy and probability frequencies for exact annotation versions"
    operation: Literal["calibration"] = option("operation", help="calibration", editor=ChoiceTargetField)
    question: QuestionVersion = option("--question", type=json.loads,
        help="JSON of the original question member and version digest")
    classifier: ClassifierVersion = option("--classifier", type=json.loads,
        help="JSON of the original classifier member and pinned release")

    def apply(self, ctx: Comms) -> CliResult:
        from .working_memory_annotations import WorkingMemoryAnnotations

        return WorkingMemoryAnnotations.calibration(
            ctx.root / "coordination.sqlite3", self.question, self.classifier)


@dataclass(frozen=True, kw_only=True)
class SupersedeConstraintCliCommand(CliCommand, declared_name="supersede-constraint"):
    help = "Publish a human's exact replacement for an authored constraint"
    thread: str = option("thread")
    source: MessageReference = option("--source", normalize=_message_reference)
    body: str = option("--body")
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def apply(self, ctx: Comms) -> CliResult:
        from .task_sources import CorrectionTaskChange, UserTaskSupersession

        message = ctx.messaging.send_user_message(self.thread, self.body, worktree=self.worktree,
            task=UserTaskSupersession(CorrectionTaskChange(self.source)))
        return ConstraintSupersededResult(message.reference)


@dataclass(frozen=True, kw_only=True)
class DropConstraintCliCommand(CliCommand, declared_name="drop-constraint"):
    help = "Retire a constraint by original reference, preserving its evidence"
    thread: str = option("thread")
    source: MessageReference = option("--source", normalize=_message_reference)
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def apply(self, ctx: Comms) -> CliResult:
        from .task_sources import CorrectionTaskChange, UserTaskDrop

        message = ctx.messaging.send_user_message(self.thread,
            f"Dropped constraint {self.source.seq}:{self.source.message_id}", worktree=self.worktree,
            task=UserTaskDrop(CorrectionTaskChange(self.source)))
        return ConstraintDroppedResult(message.reference)


@dataclass(frozen=True, kw_only=True)
class ExportRetainedCliCommand(CliCommand, declared_name="export-retained"):
    help = "Export current authored context from original checked sources"
    thread: str = option("thread")
    output: str = option("--output")
    overwrite: bool = option("--overwrite", default=False)

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.bus.log.retained_context(self.thread, ctx.registry).export(
            self.output, overwrite=self.overwrite)


@dataclass(frozen=True, kw_only=True)
class InboxCliCommand(CliCommand):
    help = "Undelivered messages for a thread"
    thread: str = option("--thread")

    def apply(self, ctx: Comms) -> CliResult:
        return InboxResult(tuple(ctx.bus.inbox(self.thread)))


@dataclass(frozen=True, kw_only=True)
class WatchCliCommand(CliCommand, declared_name="watch"):
    help = "Stream unread and arriving messages as JSON lines"
    thread: str = option("--thread")
    after: MessageReference | None = option("--after", default=None, type=json.loads, help="Exclusive original message reference already attempted by this receiver (JSON)")
    acknowledgment: type[WatchAcknowledgment] = option("--acknowledgment", default=OnReadWatchAcknowledgment, editor=ChoiceTargetField)

    def apply(self, ctx: Comms) -> AsyncIterator[Message]:
        return ctx.bus.watch(self.thread, acknowledgment=self.acknowledgment, after=self.after)

    def run(self, ctx: Comms, output: TextIO) -> int:
        emit = self.acknowledgment.line_output(output)
        async def stream() -> None:
            async with aclosing(self.apply(ctx)) as messages:
                async for message in messages:
                    emit(ArrivedMessage(message=message).model_dump_json())

        async def watch() -> None:
            thread = ctx.registry.require(self.thread)
            source = thread.runtime.history_source(ctx.root, thread.incarnation)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(stream())
                tasks.create_task(source.watch_requests(
                    lambda row: emit(EarlierItems.from_row(row).model_dump_json())))

        try:
            asyncio.run(watch())
        except* MaintenanceHeldError as held:  # the bridge reads it as its one held line
            raise held.exceptions[0] from None
        return 0


@dataclass(frozen=True, kw_only=True)
class AckCliCommand(CliCommand):
    help = "Mark inbox delivered"
    thread: str = option("--thread")

    def apply(self, ctx: Comms) -> CliResult:
        messages = ctx.bus.inbox(self.thread)
        return AcknowledgedResult(ctx.messaging.acknowledge(
            self.thread, tuple(message.reference for message in messages)))


@dataclass(frozen=True, kw_only=True)
class WhoCliCommand(CliCommand):
    help = "Presence: who is in the chat"

    def apply(self, ctx: Comms) -> CliResult:
        return WhoResult(ctx.views.who())


@dataclass(frozen=True, kw_only=True)
class StatusCliCommand(CliCommand):
    help = "Live activity: what each thread is doing right now"

    def apply(self, ctx: Comms) -> CliResult:
        snapshot = ctx.registry.snapshot()
        running = (
            thread for name, thread in snapshot.threads.items()
            if snapshot.statuses[name].active
        )
        activities = ctx.agents.all_activity(running, snapshot=snapshot)
        return ActiveStatusResult(tuple(
            ThreadActivityRow(thread, activity.state, activity.detail, activity.timestamp)
            for thread, activity in sorted(activities.items())))


@dataclass(frozen=True, kw_only=True)
class HistoryCliCommand(CliCommand):
    help = "Newest-first bounded history of a named target"
    target: str = option("--target", help="#channel or @peer", default=ANY_CHANNEL.name)
    me: str | None = option("--as", help="Participant, required only for DM history", default=None)
    limit: int = option("--limit", help="Maximum messages in this page", default=20)
    before: PageCursor | None = option("--before", help="Exclusive older-page cursor (JSON)", type=json.loads, default=None)

    def apply(self, ctx: Comms) -> HistoryPageResult:
        target = CommandTarget.named(self.target)
        traversal = self.before.older() if self.before is not None else OlderTraversal()
        bounds = PageBounds(traversal, self.limit)
        page = ctx.views.history_page(target, bounds, viewer=self.me)
        return target.history_result(ctx.bus.require_channel, tuple(reversed(page.messages)), page.has_older)


@dataclass(frozen=True, kw_only=True)
class ExportWireCliCommand(CliCommand, declared_name="export-wire"):
    help = "Export durable IRC/wire message history"
    output: str = option("--output", help="Destination file")
    format: SelectableWireExportFormat = option("--format", default=JsonlFormat(), editor=ChoiceTargetField)
    everything: bool = option(
        "--everything", help="Every wire message", group="scope", default=False
    )
    channel: str | None = option(
        "--channel", help="One target-owned #channel", group="scope", default=None
    )
    dm: list[str] | None = option(
        "--dm",
        nargs=2,
        metavar=("FIRST", "SECOND"),
        help="One canonical DM",
        group="scope",
        default=None,
    )
    full: bool = option(
        "--full", help="Export without a size/time cap", group="limit", default=False
    )
    max_bytes: int | None = option(
        "--max-bytes", help="Hard UTF-8 artifact byte ceiling", group="limit", default=None
    )
    last: float | None = option(
        "--last",
        type=_duration_seconds,
        metavar="DURATION",
        help="Recent window, e.g. 24h",
        group="limit",
        default=None,
    )
    overwrite: bool = option("--overwrite", help="Replace an existing output", default=False)

    def apply(self, ctx: Comms) -> CliResult:
        started_at = time.time()
        if self.everything:
            scope = EverythingScope()
        elif self.channel:
            scope = ChannelScope(self.channel)
        else:
            scope = DmScope(tuple(self.dm))
        if self.full:
            export_limit = FullLimit()
        elif self.max_bytes is not None:
            export_limit = MaxBytesLimit(self.max_bytes)
        else:
            export_limit = RecentLimit(started_at - self.last)
        exported = ctx.views.export_wire(
            self.output,
            format=self.format,
            scope=scope,
            limit=export_limit,
            overwrite=self.overwrite,
            export_started_at=started_at,
        )
        return exported


@dataclass(frozen=True, kw_only=True)
class ChannelsCliCommand(CliCommand):
    help = "Derived channel list"

    def apply(self, ctx: Comms) -> CliResult:
        return ChannelsResult(tuple(ctx.channels.channels()))


@dataclass(frozen=True, kw_only=True)
class ThreadsCliCommand(CliCommand):
    help = "List threads"
    active_only: bool = option("--active-only", default=False)

    def apply(self, ctx: Comms) -> CliResult:
        return ThreadsResult(ctx.views.list_threads(active_only=self.active_only))


@dataclass(frozen=True, kw_only=True)
class ThreadCliCommand(CliCommand):
    help = "Detail for one thread"
    name: str = option("--name")
    read: InboxRead = option(
        "--no-pending", help="Skip the inbox count for fast metadata reads",
        default_factory=CountedRead, parser_default=FieldCodec.encode(CountedRead()),
        action="store_const", const=FieldCodec.encode(UncountedRead()), normalize=dict)

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.views.thread_detail(self.name, self.read)


@dataclass(frozen=True, kw_only=True)
class RegisterCliCommand(CliCommand):
    help = "Register a thread"
    name: str = option("--name")
    worktree: str = option("--worktree")
    tags: frozenset[str] = option(
        "--tags", default_factory=frozenset, parser_default="", normalize=_tags
    )
    parent: str | None = option("--parent", default=None)
    task: str | None = option("--task", default=None)
    process: ProcessState = option("--pid", default_factory=NoProcess,
        parser_default=FieldCodec.encode(NoProcess()), type=ProcessIdentity.pid_argument)
    execution: type[ThreadExecution] = option("--execution", default=ExternalThreadExecution, editor=ChoiceTargetField,
                                            parser_default=FieldCodec.encode(ExternalThreadExecution))

    def apply(self, ctx: Comms) -> CliResult:

        thread = ctx.registry.new_thread(
            name=self.name,
            tags=self.tags,
            worktree=self.worktree,
            parent=self.parent,
            task=self.task,
            process_identity=self.process,
            execution=self.execution,
        )
        ctx.registry.declare(thread)
        return RegisteredResult(self.name)


@dataclass(frozen=True, kw_only=True)
class InstallCliCommand(CliCommand):
    help = "Set up every coding harness on this machine to use this bus, with the commander skill and tools"
    project: Annotated[Path, PathText] = option("--project", default_factory=Path.cwd, parser_default=".")
    repos: tuple[Annotated[Path, PathText], ...] = option("--repos", default=(), parser_default="", normalize=_tags)
    commander: str = option("--commander", default="claude-code")

    def apply(self, ctx: Comms) -> CliResult:
        from .commander.config import CommanderConfig

        home = Path.home()
        project = self.project.resolve()
        config = CommanderConfig(
            commander=self.commander, root=ctx.root,
            state=home / ".local/state/agent-comms/commander",
            board=project / "docs/agents/fleet-board.md", narrative=project / "docs/agents/fleet-narrative.md",
            repos=tuple(Repo.of(repo) for repo in self.repos or (project,)),
            worktrees=home / "wt", scratch=home / ".cache/agent-scratch", disk=home,
        )
        return InstallResult(str(config.write(home)), {
            type(harness).declared_name: harness.install(ctx, self.commander, project)
            for harness in Harness.present(home)})


@dataclass(frozen=True, kw_only=True)
class HeartbeatCliCommand(CliCommand):
    help = "Mark thread running"
    name: str = option("--name")

    def apply(self, ctx: Comms) -> CliResult:
        ctx.threads.heartbeat(self.name)
        return HeartbeatResult(self.name)


@dataclass(frozen=True, kw_only=True)
class AttachSessionCliCommand(CliCommand, declared_name="attach-session"):
    help = "Attach a live Pi session to a registered thread"
    name: str = option("--name")
    session_file: str = option("--session-file")
    process: ProcessState = option("--pid", default_factory=NoProcess,
        parser_default=FieldCodec.encode(NoProcess()), type=ProcessIdentity.pid_argument)

    def apply(self, ctx: Comms) -> CliResult:
        current = ctx.registry.require(self.name)
        attached = ctx.threads.attach_session(
            current, self.session_file,
            process=self.process.attachment_process(current.process_identity),
        )
        return SessionAttachedResult(
            attached.name, attached.session_file, **attached.process_identity.pid_fields())


@dataclass(frozen=True, kw_only=True)
class SchemaReportCliCommand(CliCommand, declared_name="schema-report"):
    help = "Report declared DDL differences in existing SQLite stores (including retained copies)"

    def apply(self, ctx: Comms) -> CliResult:
        return StoreSchemaReports(StoreSchemaReport.scan(ctx))


@dataclass(frozen=True, kw_only=True)
class CheckpointCliCommand(CliCommand, declared_name="checkpoint"):
    help = "Commit and push a thread's uncommitted worktree changes as a checkpoint branch"
    name: str = option("--name")

    def apply(self, ctx: Comms) -> CliResult:
        import subprocess

        worktree = Path(ctx.registry.require(self.name).worktree).resolve()
        branch = f"checkpoint/{self.name}"
        # Git requires an absolute ceiling. Stop before entering the declared
        # worktree's parent, never checkpoint an ancestor repository.
        environment = {**os.environ, "GIT_CEILING_DIRECTORIES": str(worktree.parent)}

        def git(*arguments: str, timeout: int = 60):
            done = subprocess.run(["git", "-C", str(worktree), *arguments],
                                  capture_output=True, text=True, timeout=timeout,
                                  env=environment)
            return done.returncode, done.stdout, done.stderr

        code, out, _ = git("rev-parse", "--is-inside-work-tree")
        if code != 0 or out.strip() != "true":
            return CheckpointResult(
                self.name, "refused", branch,
                reason="the thread declaration's worktree is not a git worktree")
        if not git("status", "--porcelain")[1].strip():
            return CheckpointResult(self.name, "clean", branch)
        git("add", "-A")
        code, out, err = git("commit", "-m", f"Checkpoint {self.name} before compaction")
        if code != 0 and "nothing to commit" in (out + err):
            return CheckpointResult(self.name, "clean", branch)
        if code != 0:
            return CheckpointResult(self.name, "failed", branch,
                                    reason=(err.strip() or out.strip())[:400])
        # The transport can fail mid-flight; the named state is what survives,
        # never a silent skip and never a block on compaction.
        code, out, err = git("push", "--force", "origin", f"HEAD:refs/heads/{branch}", timeout=120)
        if code != 0:
            return CheckpointResult(self.name, "push-unknown", branch,
                                    reason=(err.strip() or out.strip())[:400])
        commit = git("rev-parse", "--short", "HEAD")[1].strip()
        return CheckpointResult(self.name, "pushed", branch, commit=commit)


@dataclass(frozen=True, kw_only=True)
class ActivityCliCommand(CliCommand):
    help = "Publish current thread activity"
    name: str = option("--name")
    state: ActivityState = option("--state", editor=ChoiceTargetField)
    detail: str = option("--detail", default="")

    def apply(self, ctx: Comms) -> CliResult:
        ctx.agents.set_activity(self.name, self.state, self.detail)
        return ActivitySetResult(self.name, self.state.value)


@dataclass(frozen=True, kw_only=True)
class ThreadBoundCliCommand(ThreadCommandAvailability, CliCommand):
    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus,
                        channel: str | None = None, *,
                        catalog: CatalogDocument | None = None) -> tuple[Self, ...]:
        return (cls.thread_binding(thread),) if cls.available_for_thread(thread, status) else ()

    @classmethod
    @abstractmethod
    def thread_binding(cls, thread: Thread) -> Self: ...


@dataclass(frozen=True, kw_only=True)
class ThreadLifecycleCliCommand(ThreadBoundCliCommand):
    multiple_targets = True
    name: str = option("--name", target_bound=True)

    @classmethod
    def thread_binding(cls, thread):
        return cls(name=thread.name)

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog) -> tuple[Self, ...]:
        return tuple(bound for member in resolved.members
                     for bound in cls.thread_bindings(
                         comms, member.thread, member.status, resolved.channel.name, catalog=catalog))


@dataclass(frozen=True, kw_only=True)
class StopCliCommand(NativeOwnerCommand, OwnerLifecycleControl, ThreadLifecycleCliCommand):
    help = "Stop process"

    def apply(self, ctx: Comms) -> ThreadStoppedResult:
        ctx.owners.stop(self.name)
        return ThreadStoppedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class RestartCliCommand(CliCommand):
    help = "Restart process"
    name: str | None = option(
        "--name", help="One running thread (aliases supported)", group="scope", default=None,
        target_bound=True,
    )
    all_: bool = option(
        "--all",
        help="All running owners; excludes stopped threads",
        group="scope",
        default=False,
        wire_name="all",
        target_bound=True,
    )
    agent_bin: str | None = option("--agent-bin", default=None)
    agent_args: list[str] | None = option(
        "--agent-args",
        default=None,
        normalize=_shell_words,
    )

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        # A selected thread restart retains its existing launch declaration.
        # Explicit CLI launch overrides are independent operator arguments.
        return ()

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        if thread.turn_record.read().executing:
            return ()
        return (cls(name=thread.name),) if thread.restart_candidates(status) else ()

    def apply(self, ctx: Comms) -> CliResult:
        results = ctx.owners.restart_owners(
            None if self.all_ else [self.name], agent_bin=self.agent_bin, agent_args=self.agent_args
        )
        return OwnersRestartedResult(results)


@dataclass(frozen=True, kw_only=True)
class ReleaseCliCommand(CliCommand):
    help = "Voluntarily mark the calling thread stopped"
    name: str = option("--name")

    def apply(self, ctx: Comms) -> CliResult:
        ctx.owners.release(self.name)
        return OwnerReleasedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class ArchiveCliCommand(ArchiveOwnerControl, ThreadBoundCliCommand):
    help = "Archive a stopped thread"
    multiple_targets = True
    name: str = option("--name", target_bound=True)

    @classmethod
    def thread_binding(cls, thread):
        return cls(name=thread.name)

    def apply(self, ctx: Comms) -> ThreadArchivedResult:
        ctx.threads.archive(self.name)
        return ThreadArchivedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class DeleteCliCommand(CliCommand, declared_name="delete"):
    help = "Permanently delete a stopped or archived thread; its bus history stays"
    name: str = option("--name")

    def apply(self, ctx: Comms) -> ThreadDeleted:
        return ctx.threads.delete(self.name)




@dataclass(frozen=True, kw_only=True)
class NewThreadCliCommand(CliCommand, declared_name="new"):
    help = "Create a new pi thread"
    spec_type: ClassVar[type[NewThreadSpec]] = NewThreadSpec
    name: str = option("--name")
    task: str = option("--task", default="", multiline=True)
    tags: frozenset[str] | None = option(
        "--tags", default=None, editor_format=_tag_text, normalize=lambda value: _tags(value) if value is not None else None
    )
    project: str = option("--project", parser_default_factory=os.getcwd)
    model: str | None = option("--model", parser_default=NativeThreadDefaults.model,
                        catalog=ModelConfigOption)
    thinking_level: ThinkingLevelField = option(
        "--thinking-level", default=NativeThreadDefaults.thinking_level,
        catalog=ThinkingLevelConfigOption)

    @classmethod
    def project_bindings(cls, comms, project):
        defaults = NativeThreadDefaults()
        return (cls(name='', project=project, model=defaults.model,
                    thinking_level=defaults.thinking_level),)

    def apply(self, ctx: Comms) -> ThreadCreatedResult:
        asyncio.run(ConfigOptions.configured().validate(self))
        spec = self.spec_type.from_task(**{item.name: getattr(self, item.name)
                                           for item in fields(self.spec_type)})
        child = ctx.threads.create(spec)
        return ThreadCreatedResult(child.name, child.process_identity)


@dataclass(frozen=True, kw_only=True)
class ForkCliCommand(NativeOwnerCommand, ThreadBoundCliCommand, NewThreadCliCommand):
    help = "Fork a child pi thread"
    spec_type = ForkSpec
    parent: str = option("--parent", target_bound=True)

    @classmethod
    def project_bindings(cls, comms, project):
        return ()

    @classmethod
    def thread_binding(cls, thread):
        return cls(name='', parent=thread.name, tags=thread.tags, project=thread.worktree,
                   model=thread.model, thinking_level=thread.thinking_level)


@dataclass(frozen=True, kw_only=True)
class LedgerCliCommand(CliCommand):
    help = "Read or merge ledger state"
    merge_from: str | None = option("--merge-from", default=None)
    author: str | None = option("--author", default=None)

    def apply(self, ctx: Comms) -> CliResult:
        if self.merge_from is not None:
            if not self.author:
                raise ValueError("ledger merge requires --author")
            updates = json.loads(Path(self.merge_from).read_text())
            ctx.ledger.merge(updates, self.author)
            return LedgerMergedResult()
        else:
            document: LedgerDocument = ctx.ledger.read()
            return document


@dataclass(frozen=True, kw_only=True)
class PollCliCommand(CliCommand):
    help = "Status snapshot: thread, inbox, ledger, peers"
    # --thread names a thread; without it the poll is the calling thread's. Both parse to records.
    thread: PollTarget = option("--thread", default_factory=CallingThreadTarget, action="store",
        parser_default=FieldCodec.encode(CallingThreadTarget()), type=NamedThreadTarget.argument,
        normalize=dict)

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.views.poll(self.thread)


@dataclass(frozen=True, kw_only=True)
class ContextCliCommand(CliCommand):
    help = "Inspect original context contributors without sending an input"
    thread: str = option("thread")
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        super().add_parser(subparsers)
        parser = subparsers.choices[cls.declared_name]
        parser.add_argument("--turn", type=int, default=argparse.SUPPRESS)
        parser.add_argument("--diff", action="store_true")
        parser.add_argument("--imported", action="store_true")

    @classmethod
    def from_arguments(cls, args: argparse.Namespace) -> ContextCliCommand:
        if args.imported:
            if "turn" in vars(args) or args.diff:
                raise ValueError("--imported cannot be combined with --turn or --diff")
            return ImportedContextCliCommand(thread=args.thread)
        if "turn" in vars(args):
            command = TurnDiffContextCliCommand if args.diff else TurnContextCliCommand
            return command(thread=args.thread, turn=args.turn)
        return (DiffContextCliCommand if args.diff else cls)(thread=args.thread)

    def manifests(self, ctx: Comms) -> Iterator[ContextManifest]:
        cursor = None
        while True:
            page = ctx.bus.log.context_manifest_page(self.thread, ctx.registry, cursor=cursor)
            yield from (resource.value for resource in reversed(page.resources))
            if not page.has_older():
                return
            cursor = page.older

    def apply(self, ctx: Comms) -> CliResult:
        owner = ctx.registry.require(self.thread)
        connection = RuntimeConnection(ctx, owner.name, socket_path(ctx.root, owner.require_process().pid))

        async def inspect():
            try:
                context = FieldCodec.decode(TurnContext, await connection.request("context_core"))
                if context.thread != owner.incarnation:
                    raise ValueError("Core context belongs to another owner incarnation")
                try:
                    payload = await connection.request("context")
                    native = FieldCodec.decode(NativeContextData, payload).require_session_file(
                        owner.runtime.path)
                except RequestError as error:
                    return context, None, str(error)
                return context, native, None
            finally:
                await connection.close()

        context, native, native_error = asyncio.run(inspect())
        segments = tuple(CoreSegmentText(type(segment), segment.provenance, segment.text())
                         for segment in context.segments)
        if native is None:
            return CoreContextInspection("current-core-contributors; before future input",
                                         False, context, segments, native_error)
        native_context = native.for_turn(owner, NextContextTurn())
        return NativeContextInspection(
            "next-native-base-and-core-contributors; before future input and provider hooks",
            False, context, segments, native_context.segments, native_context.render().provider,
            tuple(NativeSourceSpans(position, segment.measured_manifest().sha256,
                                    segment.source_ranges())
                  for position, segment in enumerate(native.segments)))


class ContextOptionCliCommand(ContextCliCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        """These commands are selected by the context parser's options."""

    @abstractmethod
    def apply(self, ctx: Comms) -> CliResult:
        """Inspect the context selected at the CLI boundary."""


class ImportedContextCliCommand(ContextOptionCliCommand):
    def apply(self, ctx: Comms) -> ImportedContextSources:
        owner = ctx.registry.require(self.thread)
        return ImportedContextSources(
            "historical-imported-instructions; not current or recorded native context",
            ImportedSessionMetadata.sources_for_owner(ctx.registry, owner))


@dataclass(frozen=True, kw_only=True)
class SelectedContextCliCommand(ContextOptionCliCommand):
    turn: int

    def manifests(self, ctx: Comms) -> Iterator[ContextManifest]:
        return dropwhile(lambda manifest: not manifest.turn.matches_generation(self.turn),
                         super().manifests(ctx))


class TurnContextCliCommand(SelectedContextCliCommand):
    def apply(self, ctx: Comms) -> RecordedContextManifests:
        selected = tuple(takewhile(lambda manifest: manifest.turn.matches_generation(self.turn),
                                   self.manifests(ctx)))
        if not selected:
            raise ValueError("No original context manifest exists for the requested turn")
        return RecordedContextManifests(
            tuple(reversed(selected)), all(manifest.public_text_recorded for manifest in selected))


class DiffContextCliCommand(ContextOptionCliCommand):
    def apply(self, ctx: Comms) -> ContextManifestChange:
        manifests = iter(self.manifests(ctx))
        try:
            current = next(manifests)
        except StopIteration:
            raise ValueError("No original context manifest exists for the requested turn") from None
        for previous in manifests:
            if not current.turn.same_recording(previous.turn):
                return current.changed_since(previous)
        raise ValueError("No preceding recorded turn exists for comparison")


class TurnDiffContextCliCommand(SelectedContextCliCommand, DiffContextCliCommand):
    """Start the shared diff walk at the requested turn."""


@dataclass(frozen=True, kw_only=True)
class TargetActionsCliCommand(CliCommand, declared_name='target-actions'):
    project: str = option('--project', default_factory=os.getcwd)
    channel: str | None = option('--channel', default=None)
    help = 'List applicable operations and their declared parameters'
    target: tuple[str, ...] = option('--target', '--targets', nargs='+', default_factory=tuple,
        normalize=list)

    def apply(self, ctx: Comms) -> TargetCatalogResult:
        return TargetCatalogResult(CliCommand.target_catalog(
            ctx, CliCommand.selected_targets(CommandTarget.arguments(self.target, self.project)),
            ChannelSelection.decode(self.channel), project=self.project))


@dataclass(frozen=True, kw_only=True)
class TargetActionCliCommand(CliCommand, declared_name='target-action'):
    channel: str | None = option('--channel', default=None)
    help = 'Execute a declared operation against its current target'
    project: str = option('--project', default_factory=os.getcwd)
    target: tuple[str, ...] = option('--target', '--targets', nargs='+', default_factory=tuple,
        normalize=list)
    operation: str = option('--operation')
    arguments: dict[str, Any] = option('--arguments', default_factory=dict,
                                     parser_default='{}')
    confirmed: bool = option('--confirmed', default=False)

    def apply(self, ctx: Comms) -> CliResult:
        declaration = CliCommand.member_named(self.operation)
        return declaration.execute_target(
            ctx, declaration.selected_targets(CommandTarget.arguments(self.target, self.project)), self.arguments,
            confirmed=self.confirmed, channel=ChannelSelection.decode(self.channel), project=self.project)


@dataclass(frozen=True, kw_only=True)
class StartCliCommand(NativeOwnerCommand, StartOwnerControl, ThreadLifecycleCliCommand):
    help = 'Start thread'

    def apply(self, ctx: Comms) -> OwnerStartResult:
        return ctx.owners.start(self.name)


@dataclass(frozen=True, kw_only=True)
class ThreadTagsCliCommand(CliCommand, declared_name='thread-tags'):
    help = 'Edit thread tags'
    name: str = option('--name', target_bound=True)
    tags: frozenset[str] = option('--tags', normalize=_tags, editor_format=_tag_text,
                                help='Complete tags, separated by commas')

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        return (cls(name=thread.name, tags=thread.tags),)

    def apply(self, ctx: Comms) -> ThreadTagsResult:
        thread = ctx.channels.replace_tags(self.name, self.tags)
        return ThreadTagsResult(thread.name, thread.tags)


@dataclass(frozen=True, kw_only=True)
class ExactTagCliCommand(CliCommand):
    name: str = option('--name', target_bound=True)

    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name)

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return (cls.for_tag(resolved.channel.name.removeprefix('#')),) if resolved.channel.exact else ()


@dataclass(frozen=True, kw_only=True)
class RenameTagCliCommand(ExactTagCliCommand, declared_name='rename-tag'):
    help = 'Rename channel tag'
    new_name: str = option('--to', help='New tag name')

    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name, new_name='')

    def apply(self, ctx: Comms) -> TagRenamedResult:
        ctx.channels.rename_tag(self.name, self.new_name)
        return TagRenamedResult(self.name, self.new_name)


@dataclass(frozen=True, kw_only=True)
class DeleteTagCliCommand(ExactTagCliCommand, declared_name='delete-tag'):
    help = 'Remove tag, archive tagged threads, or delete tagged threads'
    multiple_targets = True
    disposition: TagDisposition = option('--disposition', default_factory=KeepThreadsTagDisposition, editor=ChoiceTargetField,
                                        help='Choose what happens to tagged threads')
    confirmed: bool = option('--confirmed', default=False, target_bound=True)

    def confirmation(self):
        return self.disposition.confirmation(self.name)

    def selection_confirmation(self, commands):
        groups: dict[TagDisposition, list[str]] = {}
        for command in commands:
            groups.setdefault(command.disposition, []).append(command.name)
        return '\n'.join(disposition.confirmation(tuple(names))
                         for disposition, names in groups.items())

    def with_confirmation(self, confirmed: bool) -> Self:
        super().with_confirmation(confirmed)
        return replace(self, confirmed=confirmed)

    def apply(self, ctx: Comms) -> TagChangeResult:
        return ctx.channels.delete_tag(self.name, disposition=self.disposition, confirmed=self.confirmed)


@dataclass(frozen=True, kw_only=True)
class ArchiveChannelCliCommand(CliCommand, declared_name='archive-channel'):
    help = 'Archive channel'
    multiple_targets = True
    name: str = option('--name', target_bound=True)
    archived: bool = option('--archived', default=True, target_bound=True)

    @classmethod
    def catalog_declaration(cls):
        return ArchiveCliCommand

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return (cls(name=resolved.channel.name),) if resolved.channel.can_set_archived(cls.archived) else ()

    def confirmation(self):
        return (f"Archive {self.name}? Hide the channel without removing threads, tags or history. It can be restored."
                if self.archived else '')

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_archived(self.name, self.archived)


@dataclass(frozen=True, kw_only=True)
class RestoreChannelCliCommand(ArchiveChannelCliCommand, declared_name='restore-channel'):
    help = 'Restore archived channel'
    archived: bool = option('--archived', default=False, target_bound=True)

    @classmethod
    def catalog_declaration(cls):
        return cls


@dataclass(frozen=True, kw_only=True)
class DeleteViewCliCommand(CliCommand, declared_name='delete-view'):
    help = 'Delete saved view'
    name: str = option('--name', target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return resolved.channel.delete_view_bindings(cls)

    def confirmation(self):
        return f"Delete saved view #{self.name}? Thread tags and messages are preserved."

    def apply(self, ctx: Comms) -> ViewDeletedResult:
        ctx.channels.delete_saved_view(self.name)
        return ViewDeletedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class PinChannelCliCommand(CliCommand, declared_name='pin-channel'):
    help = 'Set channel pin'
    multiple_targets = True
    name: str = option('--name', target_bound=True)
    pinned: bool = option('--pinned', default=False, target_bound=True)

    @classmethod
    def catalog_declaration(cls):
        return PinThreadCliCommand

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return (cls(name=resolved.channel.name, pinned=not resolved.channel.pinned),) if resolved.channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_pinned(self.name, self.pinned)


@dataclass(frozen=True, kw_only=True)
class ChannelActivityCliCommand(CliCommand, declared_name='channel-activity'):
    help = 'Toggle member activity'
    name: str = option('--name', target_bound=True)
    enabled: bool = option('--enabled', default=False, target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return (cls(name=resolved.channel.name, enabled=not resolved.channel.any_mode),) if resolved.channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_any_mode(self.name, self.enabled)


@dataclass(frozen=True, kw_only=True)
class TargetEditCliCommand(TargetActionCliCommand, declared_name='target-edit'):
    help = 'Execute a declared operation using its original CLI field parsers'

    def apply(self, ctx: Comms) -> CliResult:
        declaration = CliCommand.member_named(self.operation)
        return TargetEdit(declaration,
                          declaration.selected_targets(CommandTarget.arguments(self.target, self.project)), self.arguments,
                          self.confirmed, ChannelSelection.decode(self.channel), self.project).apply(ctx)


@dataclass(frozen=True, kw_only=True)
class ReadTargetCliCommand(CliCommand, declared_name='read-target'):
    help = 'Mark view read'
    multiple_targets = True
    target: str = option('--target', target_bound=True)
    worktree: str = option('--worktree', default_factory=os.getcwd, target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        return (cls(target=thread.name),)

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return (cls(target=resolved.channel.name),)

    def for_editor(self, comms, target, project):
        return replace(self, worktree=project)

    def apply(self, ctx: Comms) -> TargetReadResult:
        ctx.views.mark_user_view_read(self.target, worktree=self.worktree)
        return TargetReadResult(self.target)


@dataclass(frozen=True, kw_only=True)
class PinThreadCliCommand(CliCommand, declared_name='pin-thread'):
    help = 'Toggle thread pin in channel'
    multiple_targets = True
    name: str = option('--name', target_bound=True)
    channel: str = option('--channel', target_bound=True)
    pinned: bool = option('--pinned', default=False, target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog: CatalogDocument):
        if channel is None:
            return ()
        with comms.bus.opened_activity() as activity:
            if not catalog.require_channel(channel, activity).matches(thread.tags):
                return ()
        return (cls(name=thread.name, channel=channel,
                    pinned=thread.name not in catalog.pinned_threads(channel)),)

    def apply(self, ctx: Comms) -> ThreadPinnedResult:
        ctx.channels.set_thread_pinned(self.channel, self.name, self.pinned)
        return ThreadPinnedResult(self.name, self.pinned)


@dataclass(frozen=True)
class RouteReplacedResult(CliResult):
    root: str
    backup: str


class ReplaceRouteCliCommand(CliCommand, declared_name=ActiveRoute.repair_command):
    help = ("Replace an old-version or unreadable active route file with a route to --root; "
            "the old file is kept beside it")

    def apply(self, ctx: Comms) -> CliResult:
        marker = WireLog(ctx.root / "bus.jsonl").private_marker_unlocked()
        route = ActiveRoute(ctx.root, marker.root_id)
        return RouteReplacedResult(str(ctx.root), str(replace_unreadable_active_route(route)))
