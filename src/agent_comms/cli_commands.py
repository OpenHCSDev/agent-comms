"""CLI commands own their options, boundary decoding and operation bodies.

The parser is a projection of dataclass fields; DeclaredFamily is the sole
command catalog. Existing CLI spellings belong to the store_files.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field, fields, replace
from functools import partial
from inspect import iscoroutinefunction, signature
from itertools import dropwhile, takewhile
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, TextIO

from acp.exceptions import RequestError
from agent_comms.errors import ToolRefusedError
from agent_comms.message_bus import HeldBusWrite
from agent_comms.lifecycle_capability import LifecycleCapability, Successful

from .active_route import (
    ActiveRoute,
    CommsRoute,
    RouteFile,
    active_route_path,
    replace_unreadable_active_route,
)
from .activity import ActivityState
from .catalog_document import CatalogDocument
from .channel_management import KeepThreadsTagDisposition, TagChangeResult, TagDisposition
from .channel_targets import (
    ChannelSelection, CommandTarget, NoChannel, OneChannel, ResolvedChannel,
    ResolvedThread, SurroundingChannel,
)
from .channels import AnyChannel, Channel
from .check_root import CheckRoot
from .child_process import NoProcess, ProcessIdentity, ProcessState
from .cli_bridges.claude import ClaudeTranscript
from .cli_bridges.history import (
    AnySession, DefaultAddress, ExpectedSession, HarnessAddress, History, OfflineHistory, OnlineHistory,
)
from .cli_bridges.shared import ChoiceTargetField, DeclaredOptions, SendRequest, TargetField
from .cli_protocol import ArrivedMessage, EarlierItems
from .cli_result import CliResult
from .command import Command
from .comms import Comms, client_wire
from .config_options import (
    ConfigOptions,
    ModelConfigOption,
    ThinkingLevelConfigOption,
)
from .context_store import LATEST_CONTEXT, ContextManifestCursor
from .coordination_database import CoordinationStore
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .exporting import (
    EverythingScope,
    FullLimit,
    JsonlFormat,
    LimitFlag,
    ScopeFlag,
    SelectableWireExportFormat,
    UnsetLimitFlag,
    UnsetScopeFlag,
)
from .field_codec import (
    AbsolutePathText,
    FieldCodec,
    FieldPlan,
    PathText,
    WireForm,
    Argument,
    Catalog,
    Editor,
    EditorFormat,
    FactoryArgumentDefault,
    Multiline,
    NamedArgumentGroup,
    Normalize,
    TargetBound,
    WireName,
    WireOmitDefault,
    boundary_decoder,
)
from agent_comms.launch_root import LaunchRoot
from .harness_install import Harness
from .history_result import HistoryPageResult
from .history_views import CallingThreadTarget, NamedThreadTarget, PollTarget
from .importing import ImportedSessionMetadata, ImportLimits, ThreadImported
from .maintenance_barrier import MaintenanceHeldError
from .message_page import PageBounds
from .message_reference import FirstUnreadStart, InboxStart, MessageReference
from .messages import (
    Message,
)
from .native_settings import NativeThreadDefaults
from .native_turn_context import NativeContextData
from .owner_lifecycle import OwnerRestartResult, OwnerStartResult
from .restart_scope import AllOwners, OneOwner, RestartTarget
from .page_traversal import NewestCursor, PageCursor
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .presentation import CountedRead, InboxRead, ThreadRecord, ThreadView, UncountedRead
from .read_basis import AnonymousViewer, DisplayViewer, OnReadWatchAcknowledgment, WatchAcknowledgment
from .registry_document import RegistrySnapshot
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
from .thread_text import NoParent, NoThreadText, ThreadParent, ThreadText
from .commander.config import (
    CommanderChoice, CommanderConfigFile, CommanderRecord, KeptCommander, KeptRepos, RepoChoice,
)
from .install_choices import InstallerChoice, UnnamedInstaller
from .thread_model import ThreadModel
from .thread_management import (
    ForkSpec, GivenTags, ImportDirectory, InheritedTags, NewThreadSpec, SourceDirectory, TagChoice,
    ThreadDeleted,
)
from .thread_status import ThreadStatus
from .threads import KeptModel, ModelChoice, NextTurnConfiguration, Thread
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
from .native_arguments import ConfiguredBinary, InheritedArguments, LaunchArguments, LaunchBinary


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


@boundary_decoder
def _tag_choice_text(value: str | None) -> str | None:
    """Command-line and editor text is the given tags' wire; no text inherits."""
    return value


def _tag_text(value: frozenset[str] | None) -> str | None:
    return None if value is None else ', '.join(sorted(value))


def _shell_words(value: str) -> list[str]:
    return shlex.split(value)


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


class TargetOutcome(LifecycleCapability):
    """One attempted selection member; failure never claims absence of effects."""

    def reconnect_targets(self) -> tuple[str, ...]:
        return ()

    def open_targets(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class TargetCompleted(Successful, TargetOutcome):
    target: str
    command: CliCommand
    result: Annotated[CliResult, WireForm]

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
    def completed_count(self) -> int:
        return sum(len(outcome.items_with(Successful, outcome)) for outcome in self.outcomes)

    @property
    def successful(self) -> bool:
        return self.completed_count == len(self.outcomes)

    def reconnect_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for outcome in self.outcomes
                                   for name in outcome.reconnect_targets()))

    def open_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for outcome in self.outcomes
                                   for name in outcome.open_targets()))

    def exit_code(self) -> int:
        return int(not self.successful)


class CommandSelectionChanged(ToolRefusedError):
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

    def command_members(self, family: type[CliCommand]) -> tuple[type[CliCommand], ...]:
        """Derive menu membership from cardinality and declared capability."""
        return family.members_with(MultipleTargets if len(self.targets) > 1 else family)

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
        declaration.require_batch()
        declarations = (tuple(member for member in CliCommand.members_with(CliCommand)
                              if member.catalog_declaration() is declaration)
                        if declaration.catalog_declaration() is declaration else (declaration,))
        outcomes: list[tuple[int, TargetCompleted | TargetFailed]] = []
        planned: dict[CliCommand, tuple[int, CommandTarget, str | None]] = {}
        together: dict[type[AppliedTogether], list[tuple[int, CommandTarget, AppliedTogether]]] = {}
        for name in self.targets:
            try:
                resolved = name.resolve(snapshot, comms.bus.require_channel)
            except ToolRefusedError as error:
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
                except ToolRefusedError as error:
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
                if isinstance(command, AppliedTogether):
                    together.setdefault(type(command), []).append((position, name, command))
                    continue
                result = TargetCompleted(name.name, command, command.apply(comms))
            except ToolRefusedError as error:
                result = TargetFailed(name.name, type(error).__name__, str(error))
            outcomes.append((position, result))
        for declaration, members in together.items():
            results = declaration.apply_together(comms, tuple(command for _, _, command in members))
            outcomes.extend((position, TargetCompleted(name.name, command, result))
                            for (position, name, command), result in zip(members, results, strict=True))
        return TargetBatchResult(tuple(outcome for _, outcome in sorted(outcomes, key=lambda item: item[0])))


class AppliedTogether:
    """A command whose bindings in one target batch are applied as one operation."""

    @classmethod
    @abstractmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[CliResult, ...]: ...


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
    model: ThreadModel
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
    process: Annotated[ProcessState, WireOmitDefault()] = dataclasses.field(
        default_factory=NoProcess
    )


@dataclass(frozen=True)
class CheckpointResult(CliResult, DeclaredFamily, affix="Checkpoint"):
    """What one checkpoint did with a thread's uncommitted work."""

    family_discriminator = "checkpoint"
    thread: str
    branch: str


@dataclass(frozen=True)
class CleanCheckpoint(CheckpointResult):
    """The worktree had nothing uncommitted."""


@dataclass(frozen=True)
class PushedCheckpoint(CheckpointResult):
    commit: str


@dataclass(frozen=True)
class StoppedCheckpoint(CheckpointResult, declared_name=None):
    """A checkpoint that stopped before a named push, and why."""

    reason: str


@dataclass(frozen=True)
class RefusedCheckpoint(StoppedCheckpoint):
    """The declared worktree is not a git worktree."""


@dataclass(frozen=True)
class FailedCheckpoint(StoppedCheckpoint):
    """The commit failed."""


@dataclass(frozen=True)
class PushUnknownCheckpoint(StoppedCheckpoint):
    """The commit exists locally; the push could not be named."""


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
    config: CommanderConfigFile
    registered: dict[str, tuple[str, ...]]


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


class CliExecution(Command):
    """The dispatcher contract, including methods selected through multiple inheritance."""

    @abstractmethod
    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Execute against the selected route and write to the caller's stream."""

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        expected, actual = (
            (iscoroutinefunction(method),
             tuple((parameter.kind, parameter.default)
                   for parameter in signature(method, follow_wrapped=False).parameters.values()))
            for method in (CliExecution.execute, cls.execute)
        )
        if actual != expected:
            raise TypeError(f"{cls.__qualname__}.execute{signature(cls.execute)} disagrees with "
                            f"CliExecution.execute{signature(CliExecution.execute)}")


class MultipleTargets:
    """Commands accepting a batch own its decoding and execution permission."""

    @classmethod
    def selected_targets(
        cls, raw: CommandTarget | str | tuple[CommandTarget | str, ...]
    ) -> TargetSelection:
        return TargetSelection.decode(raw, multiple=True)

    @classmethod
    def require_batch(cls) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class CliCommand(
    DeclaredFamily,
    DeclaredOptions,
    CliExecution,
    affix="CliCommand",
    discovery_package="agent_comms.tools",
):
    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        """Concrete thread/channel variants declare their shared menu operation."""
        return cls

    @classmethod
    def selected_targets(cls, raw: CommandTarget | str | tuple[CommandTarget | str, ...]) -> TargetSelection:
        """Decode a CLI or menu target input once, before command execution."""
        return TargetSelection.decode(raw, multiple=False)

    @classmethod
    def require_batch(cls) -> None:
        raise ValueError('This action requires a single target')

    @classmethod
    def channel_fields(cls) -> tuple[FieldPlan, ...]:
        """The declared target-bound channel parameters own membership scope."""
        return tuple(
            entry
            for entry in FieldCodec.argument_fields(cls)
            if entry.key == "channel" and entry.setting(TargetBound, False)
        )

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
        for member in target.command_members(cls):
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
        return tuple(
            TargetField.of(entry, entry.current(self))
            for entry in FieldCodec.argument_fields(type(self))
            if not entry.setting(TargetBound, False)
        )

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
    def published_launch(cls) -> LaunchRoot:
        """The root a command reads when no --root is given."""
        return ActiveRoute.published_launch()

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Open the bus this command reads, then run it; a command that must act before
        the bus opens overrides this."""
        with client_wire(route) as comms:
            return self.run(comms, output)

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
        bound = {
            entry.key
            for entry in FieldCodec.argument_fields(type(self))
            if entry.setting(TargetBound, False)
        }
        if bound & arguments.keys():
            raise ValueError("Target-bound parameters cannot be overridden")
        editable = {item.name for item in self.editable_fields}
        if arguments.keys() - editable:
            raise ValueError("Parameters not offered by this target command cannot be overridden")
        encoded = FieldCodec.encode(self)
        return type(self).from_payload(
            {
                **{key: encoded[key] for key in bound & encoded.keys()},
                "kind": FieldCodec.encode(type(self)),
                **arguments,
            }
        )

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
    threads: Annotated[
        tuple[str, ...],
        Normalize(_tags),
        Argument(options={"help": "Comma-separated source thread names"}),
    ]
    into: Annotated[
        Path,
        PathText,
        Argument(options={"help": "New directory under your agent scratch directory"}),
    ]
    argv: Annotated[
        tuple[str, ...],
        Argument(
            flags=("argv",),
            options={
                "nargs": argparse.REMAINDER,
                "help": "-- COMMAND ARGS; use {root} for the private root argument",
            },
        ),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        return CheckRoot(ctx, self.threads, self.into).run(self.argv)


@dataclass(frozen=True, kw_only=True)
class ImportingCliCommand(CliCommand):
    """Where an imported thread works and how much of its source conversation it keeps."""

    worktree: Annotated[
        ImportDirectory,
        Argument(
            flags=("--worktree",), options={"help": "Override the source project directory"}
        ),
    ] = SourceDirectory()
    model: Annotated[
        ModelChoice,
        Argument(
            flags=("--model",),
            options={"help": "Target Pi provider/model; not inferred from foreign model IDs"},
        ),
    ] = KeptModel()
    tags: Annotated[
        frozenset[str],
        Normalize(_tags),
        Argument(flags=("--tags",)),
    ] = dataclasses.field(default_factory=frozenset)
    max_messages: Annotated[int, Argument(flags=("--max-messages",))] = (
        200
    )
    max_characters: Annotated[int, Argument(flags=("--max-characters",))] = (
        120000
    )
    max_message_characters: Annotated[
        int, Argument(flags=("--max-message-characters",))
    ] = 12000

    def imported(self, ctx: Comms, history: History, name: str) -> ThreadImported:
        return ctx.threads.import_thread(
            history, name=name, worktree=self.worktree, model=self.model, tags=self.tags,
            limits=ImportLimits(self.max_messages, self.max_characters, self.max_message_characters),
        )


@dataclass(frozen=True, kw_only=True)
class ImportThreadCliCommand(ImportingCliCommand, declared_name="import-thread"):
    help = "Import a stored Claude Code, Codex or OpenCode session as a stopped thread"
    harness: Annotated[
        type[Harness],
        Editor(ChoiceTargetField),
        Argument(flags=("--harness",)),
    ]
    source: Annotated[
        Path,
        PathText,
        Argument(
            flags=("--source",),
            options={
                "help": "Claude Code or Codex session JSONL, OpenCode export JSON or opencode.db"
            },
        ),
    ]
    session: Annotated[
        ExpectedSession,
        Argument(
            flags=("--session-id",), options={"help": "Required for OpenCode database imports"}
        ),
    ] = AnySession()
    name: Annotated[
        str,
        Argument(flags=("--name",), options={"help": "New agent-comms thread name"}),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        return self.imported(ctx, OfflineHistory.of(
            self.harness, self.source.expanduser().resolve(), self.session), self.name)


@dataclass(frozen=True, kw_only=True)
class ImportRunningThreadCliCommand(ImportingCliCommand, declared_name="import-running-thread"):
    help = "Import a session a running Claude Code, Codex or OpenCode holds, as a stopped thread"
    harness: Annotated[
        type[Harness],
        Editor(ChoiceTargetField),
        Argument(flags=("--harness",)),
    ]
    thread: Annotated[
        str,
        Argument(
            flags=("--thread",), options={"help": "The harness's own session or thread id"}
        ),
    ]
    address: Annotated[
        HarnessAddress,
        Argument(
            flags=("--address",),
            options={"help": "Codex app-server socket, or OpenCode server URL"},
        ),
    ] = DefaultAddress()
    name: Annotated[
        str,
        Argument(flags=("--name",), options={"help": "New agent-comms thread name"}),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        return self.imported(ctx, OnlineHistory.of(
            self.harness, self.thread, self.address, self.worktree.local_directory()), self.name)


@dataclass(frozen=True, kw_only=True)
class ImportSubagentsCliCommand(ImportingCliCommand, declared_name="import-subagents"):
    help = "Import every subagent of a stored Claude Code session, each as a stopped thread"
    session: Annotated[
        Path,
        PathText,
        Argument(
            flags=("--session",), options={"help": "The Claude Code session JSONL"}
        ),
    ]
    prefix: Annotated[
        str,
        Argument(
            flags=("--prefix",),
            options={"help": "Prepended to each subagent id to name its thread"},
        ),
    ] = ""

    def apply(self, ctx: Comms) -> SubagentsImportedResult:
        return SubagentsImportedResult(tuple(
            self.imported(ctx, ClaudeTranscript(path), self.prefix + path.stem.removeprefix("agent-"))
            for path in ClaudeTranscript.subagents(self.session.expanduser().resolve())))


class PublishingCliCommand(HeldBusWrite, CliCommand, declared_name=None):
    """Wait for maintenance to clear before running a CLI publication."""

    def apply(self, ctx: Comms) -> CliResult:
        return self.write_when_open(ctx, partial(self.publish, ctx))

    @abstractmethod
    def publish(self, ctx: Comms) -> CliResult: ...


@dataclass(frozen=True, kw_only=True)
class SendCliCommand(SendRequest, PublishingCliCommand):
    # A bare annotation would inherit SendRequest's default; field() keeps --from required.
    sender: Annotated[str, Argument(flags=("--from",))] = field()

    def publish(self, ctx: Comms) -> MessageSentResult:
        return MessageSentResult(self.sent(ctx))


@dataclass(frozen=True, kw_only=True)
class UserSendCliCommand(PublishingCliCommand, declared_name="user-send"):
    help = "Send as the human user to a thread (DM), #channel, or #all"
    target: Annotated[str, Argument(flags=("--to",))]
    body: Annotated[str, Argument(flags=("--body",))]
    worktree: Annotated[str, Argument(flags=("--worktree",))] = (
        dataclasses.field(default_factory=os.getcwd)
    )

    def publish(self, ctx: Comms) -> CliResult:
        message = ctx.messaging.send_user_message(self.target, self.body, worktree=self.worktree)
        return UserMessageSentResult(message.reference)


@dataclass(frozen=True, kw_only=True)
class ConfigureCliCommand(NextTurnConfiguration, CliCommand, declared_name="configure"):
    help = "Set a stopped thread's model, thinking level, or service tier for its next start"
    name: Annotated[str, Argument(flags=("--name",))]

    def apply(self, ctx: Comms) -> CliResult:
        thread = ctx.registry.require(self.name)
        if ctx.registry.status(thread.name).active:
            raise RelationViolationError(
                f"{thread.name} is running; change its settings through its session")
        asyncio.run(ConfigOptions.configured().validate(self.configured(thread)))
        updated = ctx.threads.set_thread_configuration(thread, self)
        return ThreadConfiguredResult(updated.name, updated.model, updated.thinking_level,
                                      updated.service_tier)


@dataclass(frozen=True, kw_only=True)
class PinConstraintCliCommand(CliCommand, declared_name="pin-constraint"):
    help = "Pin a checked original human message for its recipient"
    thread: Annotated[str, Argument(flags=("thread",))]
    source: Annotated[
        MessageReference,
        Normalize(_message_reference),
        Argument(flags=("--source",), options={"help": "Original sequence:message_id"}),
    ]
    worktree: Annotated[str, Argument(flags=("--worktree",))] = (
        dataclasses.field(default_factory=os.getcwd)
    )

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
    source: Annotated[
        str,
        Argument(flags=("--source",), options={"help": "Original durable input key"}),
    ]

    def original_source(self, ctx: Comms):
        from .input_disposition import InputDispositions

        return InputDispositions(ctx.root).lookup(self.source).context_provenance().require_human_input()

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.pin_input_constraint(self.thread, source, worktree=self.worktree)


@dataclass(frozen=True, kw_only=True)
class ApproveAnnotationsCliCommand(PinConstraintCliCommand, declared_name="approve-annotations"):
    help = "Approve a pinned external classifier using original human wording and an hourly request budget"
    per_hour: Annotated[
        int,
        Argument(
            flags=("--per-hour",),
            options={"help": "Maximum classifier requests in the rolling hour"},
        ),
    ]
    segments: Annotated[
        tuple[type[ContextSegment], ...],
        Argument(
            flags=("--segments",),
            options={
                "type": json.loads,
                "help": "JSON list of original segment kinds permitted for disclosure",
            },
        ),
    ]

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.approve_annotations(self.thread, source, worktree=self.worktree,
                                                per_hour=self.per_hour, segments=self.segments)


@dataclass(frozen=True, kw_only=True)
class CorrectAnnotationCliCommand(CliCommand, declared_name="correct-annotation"):
    help = "Correct one original stored model answer through the human identity owner"
    label: Annotated[
        ModelLabel, Argument(flags=("--label",), options={"type": json.loads})
    ]
    answer: Annotated[
        type[SpanAnswer],
        Editor(ChoiceTargetField),
        Argument(flags=("--answer",)),
    ]
    worktree: Annotated[str, Argument(flags=("--worktree",))] = (
        dataclasses.field(default_factory=os.getcwd)
    )

    def apply(self, ctx: Comms) -> CliResult:
        from .coordinator import Coordination

        author = ctx.messaging.user_identity(self.worktree).incarnation
        snapshot = ctx.registry.snapshot()
        with Coordination(str(CoordinationStore.path_in(ctx.root))) as store:
            return store.annotations.correct(self.label, self.answer, author, snapshot)


@dataclass(frozen=True, kw_only=True)
class AnnotationsCalibrationCliCommand(CliCommand, declared_name="annotations"):
    help = (
        "Report human-reviewed accuracy and probability frequencies for exact annotation versions"
    )
    operation: Annotated[
        Literal["calibration"],
        Editor(ChoiceTargetField),
        Argument(flags=("operation",), options={"help": "calibration"}),
    ]
    question: Annotated[
        QuestionVersion,
        Argument(
            flags=("--question",),
            options={
                "type": json.loads,
                "help": "JSON of the original question member and version digest",
            },
        ),
    ]
    classifier: Annotated[
        ClassifierVersion,
        Argument(
            flags=("--classifier",),
            options={
                "type": json.loads,
                "help": "JSON of the original classifier member and pinned release",
            },
        ),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        from .working_memory_annotations import WorkingMemoryAnnotations

        return WorkingMemoryAnnotations.calibration(
            CoordinationStore.path_in(ctx.root), self.question, self.classifier)


@dataclass(frozen=True, kw_only=True)
class SupersedeConstraintCliCommand(CliCommand, declared_name="supersede-constraint"):
    help = "Publish a human's exact replacement for an authored constraint"
    thread: Annotated[str, Argument(flags=("thread",))]
    source: Annotated[
        MessageReference,
        Normalize(_message_reference),
        Argument(flags=("--source",)),
    ]
    body: Annotated[str, Argument(flags=("--body",))]
    worktree: Annotated[str, Argument(flags=("--worktree",))] = (
        dataclasses.field(default_factory=os.getcwd)
    )

    def apply(self, ctx: Comms) -> CliResult:
        from .task_sources import CorrectionTaskChange, UserTaskSupersession

        message = ctx.messaging.send_user_message(self.thread, self.body, worktree=self.worktree,
            task=UserTaskSupersession(CorrectionTaskChange(self.source)))
        return ConstraintSupersededResult(message.reference)


@dataclass(frozen=True, kw_only=True)
class DropConstraintCliCommand(CliCommand, declared_name="drop-constraint"):
    help = "Retire a constraint by original reference, preserving its evidence"
    thread: Annotated[str, Argument(flags=("thread",))]
    source: Annotated[
        MessageReference,
        Normalize(_message_reference),
        Argument(flags=("--source",)),
    ]
    worktree: Annotated[str, Argument(flags=("--worktree",))] = (
        dataclasses.field(default_factory=os.getcwd)
    )

    def apply(self, ctx: Comms) -> CliResult:
        from .task_sources import CorrectionTaskChange, UserTaskDrop

        message = ctx.messaging.send_user_message(self.thread,
            f"Dropped constraint {self.source.seq}:{self.source.message_id}", worktree=self.worktree,
            task=UserTaskDrop(CorrectionTaskChange(self.source)))
        return ConstraintDroppedResult(message.reference)


@dataclass(frozen=True, kw_only=True)
class ExportRetainedCliCommand(CliCommand, declared_name="export-retained"):
    help = "Export current authored context from original checked sources"
    thread: Annotated[str, Argument(flags=("thread",))]
    output: Annotated[str, Argument(flags=("--output",))]
    overwrite: Annotated[bool, Argument(flags=("--overwrite",))] = (
        False
    )

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.bus.log.retained_context(self.thread, ctx.registry).export(
            self.output, overwrite=self.overwrite)


@dataclass(frozen=True, kw_only=True)
class InboxCliCommand(CliCommand):
    help = "Undelivered messages for a thread"
    thread: Annotated[str, Argument(flags=("--thread",))]

    def apply(self, ctx: Comms) -> CliResult:
        return InboxResult(tuple(ctx.bus.inbox(self.thread)))


@dataclass(frozen=True, kw_only=True)
class WatchCliCommand(CliCommand, declared_name="watch"):
    help = "Stream unread and arriving messages as JSON lines"
    thread: Annotated[str, Argument(flags=("--thread",))]
    after: Annotated[
        InboxStart,
        Argument(
            flags=("--after",),
            options={
                "type": json.loads,
                "help": "Exclusive original message reference already attempted by this receiver (JSON)",
            },
        ),
    ] = FirstUnreadStart()
    acknowledgment: Annotated[
        type[WatchAcknowledgment],
        Editor(ChoiceTargetField),
        Argument(flags=("--acknowledgment",)),
    ] = OnReadWatchAcknowledgment

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
    thread: Annotated[str, Argument(flags=("--thread",))]

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
            if snapshot.status(name).active
        )
        activities = ctx.agents.all_activity(running, snapshot=snapshot)
        return ActiveStatusResult(tuple(
            ThreadActivityRow(thread, activity.state, activity.detail, activity.timestamp)
            for thread, activity in sorted(activities.items())))


@dataclass(frozen=True, kw_only=True)
class HistoryCliCommand(CliCommand):
    help = "Newest-first bounded history of a named target"
    target: Annotated[
        str, Argument(flags=("--target",), options={"help": "#channel or @peer"})
    ] = AnyChannel.declared_name
    me: Annotated[
        DisplayViewer,
        Argument(
            flags=("--as",), options={"help": "Participant, required only for DM history"}
        ),
    ] = AnonymousViewer()
    limit: Annotated[
        int,
        Argument(flags=("--limit",), options={"help": "Maximum messages in this page"}),
    ] = 20
    before: Annotated[
        PageCursor,
        Argument(
            flags=("--before",),
            options={"help": "Exclusive older-page cursor (JSON)", "type": json.loads},
        ),
    ] = NewestCursor()

    def apply(self, ctx: Comms) -> HistoryPageResult:
        target = CommandTarget.named(self.target)
        bounds = PageBounds(self.before.older(), self.limit)
        page = ctx.views.history_page(target, bounds, viewer=self.me)
        return target.history_result(ctx.bus.require_channel, tuple(reversed(page.messages)), page.has_older)


@dataclass(frozen=True, kw_only=True)
class ExportWireCliCommand(CliCommand, declared_name="export-wire"):
    help = "Export durable IRC/wire message history"
    output: Annotated[
        str, Argument(flags=("--output",), options={"help": "Destination file"})
    ]
    format: Annotated[
        SelectableWireExportFormat,
        Editor(ChoiceTargetField),
        Argument(flags=("--format",)),
    ] = JsonlFormat()
    everything: Annotated[
        bool,
        Argument(
            flags=("--everything",),
            group=NamedArgumentGroup("scope"),
            options={"help": "Every wire message"},
        ),
    ] = False
    channel: Annotated[
        ScopeFlag,
        Argument(
            flags=("--channel",),
            group=NamedArgumentGroup("scope"),
            options={"help": "One target-owned #channel"},
        ),
    ] = UnsetScopeFlag()
    dm: Annotated[
        ScopeFlag,
        Argument(
            flags=("--dm",),
            group=NamedArgumentGroup("scope"),
            options={"nargs": 2, "metavar": ("FIRST", "SECOND"), "help": "One canonical DM"},
        ),
    ] = UnsetScopeFlag()
    full: Annotated[
        bool,
        Argument(
            flags=("--full",),
            group=NamedArgumentGroup("limit"),
            options={"help": "Export without a size/time cap"},
        ),
    ] = False
    max_bytes: Annotated[
        LimitFlag,
        Argument(
            flags=("--max-bytes",),
            group=NamedArgumentGroup("limit"),
            options={"type": int, "help": "Hard UTF-8 artifact byte ceiling"},
        ),
    ] = UnsetLimitFlag()
    last: Annotated[
        LimitFlag,
        Argument(
            flags=("--last",),
            group=NamedArgumentGroup("limit"),
            options={
                "type": _duration_seconds,
                "metavar": "DURATION",
                "help": "Recent window, e.g. 24h",
            },
        ),
    ] = UnsetLimitFlag()
    overwrite: Annotated[
        bool,
        Argument(
            flags=("--overwrite",), options={"help": "Replace an existing output"}
        ),
    ] = False

    def apply(self, ctx: Comms) -> CliResult:
        started_at = time.time()
        # The required argument groups hold exactly one flag each; --everything and --full
        # are what remains when no other flag of their group names a choice.
        exported = ctx.views.export_wire(
            self.output,
            format=self.format,
            scope=self.channel.chosen_or(self.dm.chosen_or(EverythingScope())),
            limit=self.max_bytes.chosen_or(self.last.chosen_or(FullLimit(), started_at), started_at),
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
    active_only: Annotated[bool, Argument(flags=("--active-only",))] = (
        False
    )

    def apply(self, ctx: Comms) -> CliResult:
        return ThreadsResult(ctx.views.list_threads(active_only=self.active_only))


@dataclass(frozen=True, kw_only=True)
class ThreadCliCommand(CliCommand):
    help = "Detail for one thread"
    name: Annotated[str, Argument(flags=("--name",))]
    read: Annotated[
        InboxRead,
        Normalize(dict),
        Argument(
            flags=("--no-pending",),
            options={
                "help": "Skip the inbox count for fast metadata reads",
                "action": "store_const",
                "const": FieldCodec.encode(UncountedRead()),
            },
        ),
    ] = dataclasses.field(default_factory=CountedRead)

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.views.thread_detail(self.name, self.read)


@dataclass(frozen=True, kw_only=True)
class RegisterCliCommand(CliCommand):
    help = "Register a thread"
    name: Annotated[str, Argument(flags=("--name",))]
    worktree: Annotated[str, Argument(flags=("--worktree",))]
    tags: Annotated[
        frozenset[str],
        Normalize(_tags),
        Argument(flags=("--tags",)),
    ] = dataclasses.field(default_factory=frozenset)
    parent: Annotated[ThreadParent, Argument(flags=("--parent",))] = NoParent()
    task: Annotated[ThreadText, Argument(flags=("--task",))] = NoThreadText()
    process: Annotated[
        ProcessState,
        Argument(
            flags=("--pid",),
            options={"type": ProcessIdentity.pid_argument},
        ),
    ] = dataclasses.field(default_factory=NoProcess)
    execution: Annotated[
        type[ThreadExecution],
        Editor(ChoiceTargetField),
        Argument(
            flags=("--execution",),
        ),
    ] = ExternalThreadExecution

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
    help = ("Upgrade this machine's bus, checkout and runtime to --to, then set up every coding "
            "harness to use the bus, with the commander skill and tools")
    checkout: Annotated[
        Path,
        AbsolutePathText,
        Argument(flags=("--checkout",)),
    ] = dataclasses.field(default_factory=Path.cwd)
    to: Annotated[str, Argument(flags=("--to",))] = "origin/main"
    installer: Annotated[
        InstallerChoice,
        Argument(
            flags=("--installer",),
            options={"help": "The unit running this install; the next install's plan names it"},
        ),
    ] = UnnamedInstaller()
    rehearse: Annotated[
        bool,
        Argument(flags=("--rehearse",), options={"help": "Carry and read a copy of the bus; change nothing"}),
    ] = False
    retire: Annotated[
        bool,
        Argument(
            flags=("--retire",),
            options={"help": "Move the bus aside, kept, and install a fresh one in its place"},
        ),
    ] = False
    spill_dir: Annotated[
        Path,
        AbsolutePathText,
        Argument(
            flags=("--spill-dir",),
            options={"help": "Parent directory for live backups"},
        ),
    ] = dataclasses.field(default_factory=lambda: Path.home() / ".cache/agent-rehearsal")
    project: Annotated[
        Path,
        AbsolutePathText,
        Argument(flags=("--project",), options={"help": "The worktree harness threads register in"}),
    ] = dataclasses.field(default_factory=Path.cwd)
    repos: Annotated[
        RepoChoice,
        Argument(flags=("--repos",), options={"help": "Replace the commander's recorded repositories"}),
    ] = KeptRepos()
    commander: Annotated[
        CommanderChoice,
        Argument(flags=("--commander",), options={"help": "Record this thread as the bus's commander"}),
    ] = KeptCommander()

    @classmethod
    def published_launch(cls) -> LaunchRoot:
        """Install runs where no route selects a bus yet: the published bus while its route
        still selects it, otherwise the default root, where install creates one."""
        return LaunchRoot.from_path(RouteFile.read(active_route_path()).launch_root())

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """The bus may be unreadable by this runtime, or absent, until the target's own
        source has carried or created it, so nothing opens it here."""
        from agent_comms.installer.launch import InstallRun

        return InstallRun.start(self, route.root).launch(output)

    def apply(self, ctx: Comms) -> CliResult:
        """The install child's last step, on the bus it carried or created."""
        home = Path.home()
        record = self.commander.configured(CommanderRecord.read(ctx.root), self.repos, ctx.root, home)
        return InstallResult(record.write(home), {
            type(harness).declared_name: harness.register(ctx, self.project)
            for harness in Harness.present(home)})


@dataclass(frozen=True, kw_only=True)
class HeartbeatCliCommand(CliCommand):
    help = "Mark thread running"
    name: Annotated[str, Argument(flags=("--name",))]

    def apply(self, ctx: Comms) -> CliResult:
        ctx.threads.heartbeat(self.name)
        return HeartbeatResult(self.name)


@dataclass(frozen=True, kw_only=True)
class AttachSessionCliCommand(CliCommand, declared_name="attach-session"):
    help = "Attach a live Pi session to a registered thread"
    name: Annotated[str, Argument(flags=("--name",))]
    session_file: Annotated[str, Argument(flags=("--session-file",))]
    process: Annotated[
        ProcessState,
        Argument(
            flags=("--pid",),
            options={"type": ProcessIdentity.pid_argument},
        ),
    ] = dataclasses.field(default_factory=NoProcess)

    def apply(self, ctx: Comms) -> CliResult:
        current = ctx.registry.require(self.name)
        attached = ctx.threads.attach_session(
            current, self.session_file,
            process=self.process.attachment_process(current.process_identity),
        )
        return SessionAttachedResult(attached.name, attached.session_file, attached.process_identity)


@dataclass(frozen=True, kw_only=True)
class SchemaReportCliCommand(CliCommand, declared_name="schema-report"):
    help = "Report declared DDL differences in existing SQLite stores (including retained copies)"

    def apply(self, ctx: Comms) -> CliResult:
        return StoreSchemaReports(StoreSchemaReport.scan(ctx))


@dataclass(frozen=True, kw_only=True)
class CheckpointCliCommand(CliCommand, declared_name="checkpoint"):
    help = "Commit and push a thread's uncommitted worktree changes as a checkpoint branch"
    name: Annotated[str, Argument(flags=("--name",))]

    def apply(self, ctx: Comms) -> CliResult:
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
            return RefusedCheckpoint(
                self.name, branch, "the thread declaration's worktree is not a git worktree")
        if not git("status", "--porcelain")[1].strip():
            return CleanCheckpoint(self.name, branch)
        git("add", "-A")
        code, out, err = git("commit", "-m", f"Checkpoint {self.name} before compaction")
        if code != 0 and "nothing to commit" in (out + err):
            return CleanCheckpoint(self.name, branch)
        if code != 0:
            return FailedCheckpoint(self.name, branch, (err.strip() or out.strip())[:400])
        # The transport can fail mid-flight; the named state is what survives,
        # never a silent skip and never a block on compaction.
        code, out, err = git("push", "--force", "origin", f"HEAD:refs/heads/{branch}", timeout=120)
        if code != 0:
            return PushUnknownCheckpoint(self.name, branch, (err.strip() or out.strip())[:400])
        return PushedCheckpoint(self.name, branch, git("rev-parse", "--short", "HEAD")[1].strip())


@dataclass(frozen=True, kw_only=True)
class ActivityCliCommand(CliCommand):
    help = "Publish current thread activity"
    name: Annotated[str, Argument(flags=("--name",))]
    state: Annotated[
        ActivityState,
        Editor(ChoiceTargetField),
        Argument(flags=("--state",)),
    ]
    detail: Annotated[str, Argument(flags=("--detail",))] = ""

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
class NamedCliCommand(CliCommand):
    name: Annotated[str, TargetBound(), Argument(flags=("--name",))]


@dataclass(frozen=True, kw_only=True)
class NamedThreadCliCommand(ThreadBoundCliCommand, NamedCliCommand):
    @classmethod
    def thread_binding(cls, thread: Thread) -> Self:
        return cls(name=thread.name)

    @classmethod
    def channel_members(cls, resolved: ResolvedChannel) -> tuple[ResolvedThread, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog) -> tuple[Self, ...]:
        return tuple(bound for member in cls.channel_members(resolved)
                     for bound in cls.thread_bindings(
                         comms, member.thread, member.status, resolved.channel.name, catalog=catalog))


@dataclass(frozen=True, kw_only=True)
class ThreadLifecycleCliCommand(MultipleTargets, NamedThreadCliCommand):
    @classmethod
    def channel_members(cls, resolved: ResolvedChannel) -> tuple[ResolvedThread, ...]:
        return resolved.members


@dataclass(frozen=True, kw_only=True)
class StopCliCommand(AppliedTogether, NativeOwnerCommand, OwnerLifecycleControl, ThreadLifecycleCliCommand):
    help = "Stop process"

    def apply(self, ctx: Comms) -> ThreadStoppedResult:
        return self.apply_together(ctx, (self,))[0]

    @classmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[ThreadStoppedResult, ...]:
        """Every selected owner shares one signal pass and one grace period."""
        ctx.owners.stop(*(command.name for command in commands))
        return tuple(ThreadStoppedResult(command.name) for command in commands)


@dataclass(frozen=True, kw_only=True)
class RestartCliCommand(CliCommand):
    help = "Restart process"
    name: Annotated[
        RestartTarget,
        TargetBound(),
        Argument(
            flags=("--name",),
            group=NamedArgumentGroup("scope"),
            options={"help": "One running thread (aliases supported)"},
        ),
    ] = AllOwners()
    all_: Annotated[
        bool,
        WireName("all"),
        TargetBound(),
        Argument(
            flags=("--all",),
            group=NamedArgumentGroup("scope"),
            options={"help": "All running owners; excludes stopped threads"},
        ),
    ] = False
    agent_bin: Annotated[LaunchBinary, Argument(flags=("--agent-bin",))] = ConfiguredBinary()
    agent_args: Annotated[
        LaunchArguments,
        Normalize(_shell_words),
        Argument(flags=("--agent-args",)),
    ] = InheritedArguments()

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        # A selected thread restart retains its existing launch declaration.
        # Explicit CLI launch overrides are independent operator arguments.
        return ()

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        if thread.executing:
            return ()
        return (cls(name=OneOwner(thread.name)),) if thread.restart_candidates(status) else ()

    def apply(self, ctx: Comms) -> CliResult:
        results = ctx.owners.restart_owners(self.name, agent_bin=self.agent_bin, agent_args=self.agent_args)
        return OwnersRestartedResult(results)


@dataclass(frozen=True, kw_only=True)
class ReleaseCliCommand(CliCommand):
    help = "Voluntarily mark the calling thread stopped"
    name: Annotated[str, Argument(flags=("--name",))]

    def apply(self, ctx: Comms) -> CliResult:
        ctx.owners.release(self.name)
        return OwnerReleasedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class ArchiveCliCommand(MultipleTargets, ArchiveOwnerControl, NamedThreadCliCommand):
    help = "Archive a stopped thread"

    def apply(self, ctx: Comms) -> ThreadArchivedResult:
        ctx.threads.archive(self.name)
        return ThreadArchivedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class DeleteCliCommand(CliCommand, declared_name="delete"):
    help = "Permanently delete a stopped or archived thread; its bus history stays"
    name: Annotated[str, Argument(flags=("--name",))]

    def apply(self, ctx: Comms) -> ThreadDeleted:
        return ctx.threads.delete(self.name)


@dataclass(frozen=True, kw_only=True)
class NewThreadCliCommand(CliCommand, declared_name="new"):
    help = "Create a new pi thread"
    spec_type: ClassVar[type[NewThreadSpec]] = NewThreadSpec
    name: Annotated[str, Argument(flags=("--name",))]
    task: Annotated[
        str, Multiline(), Argument(flags=("--task",))
    ] = ""
    tags: Annotated[
        TagChoice,
        EditorFormat(FieldCodec.encode),
        Normalize(_tag_choice_text),
        Argument(flags=("--tags",)),
    ] = InheritedTags()
    project: Annotated[
        str,
        Argument(
            flags=("--project",), default=FactoryArgumentDefault(os.getcwd)
        ),
    ]
    model: Annotated[
        ThreadModel, Catalog(ModelConfigOption), Argument(flags=("--model",)),
    ] = NativeThreadDefaults.model
    thinking_level: Annotated[
        ThinkingLevelField,
        Catalog(ThinkingLevelConfigOption),
        Argument(flags=("--thinking-level",)),
    ] = NativeThreadDefaults.thinking_level

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
    parent: Annotated[
        str, TargetBound(), Argument(flags=("--parent",))
    ]

    @classmethod
    def project_bindings(cls, comms, project):
        return ()

    @classmethod
    def thread_binding(cls, thread):
        return cls(name='', parent=thread.name, tags=GivenTags(thread.tags), project=thread.worktree,
                   model=thread.model, thinking_level=thread.thinking_level)


@dataclass(frozen=True, kw_only=True)
class LedgerCliCommand(CliCommand):
    help = "Read ledger state"

    def apply(self, ctx: Comms) -> LedgerDocument:
        return ctx.ledger.read()


@dataclass(frozen=True, kw_only=True)
class LedgerMergeCliCommand(CliCommand, declared_name="ledger-merge"):
    help = "Merge ledger updates from a JSON file"
    merge_from: Annotated[str, Argument(flags=("--merge-from",))]
    author: Annotated[str, Argument(flags=("--author",))]

    def apply(self, ctx: Comms) -> LedgerMergedResult:
        ctx.ledger.merge(json.loads(Path(self.merge_from).read_text()), self.author)
        return LedgerMergedResult()


@dataclass(frozen=True, kw_only=True)
class PollCliCommand(CliCommand):
    help = "Status snapshot: thread, inbox, ledger, peers"
    # --thread names a thread; without it the poll is the calling thread's. Both parse to records.
    thread: Annotated[
        PollTarget,
        Normalize(dict),
        Argument(
            flags=("--thread",),
            options={"action": "store", "type": NamedThreadTarget.argument},
        ),
    ] = dataclasses.field(default_factory=CallingThreadTarget)

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.views.poll(self.thread)


@dataclass(frozen=True, kw_only=True)
class ContextCliCommand(CliCommand):
    help = "Inspect original context contributors without sending an input"
    thread: Annotated[str, Argument(flags=("thread",))]

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
        cursor: ContextManifestCursor = LATEST_CONTEXT
        while True:
            page = ctx.contexts.page(self.thread, ctx.registry, cursor=cursor)
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
class TargetArguments(CliCommand):
    project: Annotated[str, Argument(flags=("--project",))] = dataclasses.field(
        default_factory=os.getcwd
    )
    channel: Annotated[SurroundingChannel, Argument(flags=("--channel",))] = NoChannel()
    target: Annotated[
        tuple[str, ...],
        Normalize(list),
        Argument(
            flags=(
                "--target",
                "--targets",
            ),
            options={"nargs": "+"},
        ),
    ] = dataclasses.field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True)
class TargetActionsCliCommand(TargetArguments, declared_name="target-actions"):
    help = "List applicable operations and their declared parameters"

    def apply(self, ctx: Comms) -> TargetCatalogResult:
        return TargetCatalogResult(CliCommand.target_catalog(
            ctx, CliCommand.selected_targets(CommandTarget.arguments(self.target, self.project)),
            self.channel, project=self.project))


@dataclass(frozen=True, kw_only=True)
class TargetActionCliCommand(TargetArguments, declared_name="target-action"):
    help = "Execute a declared operation against its current target"
    operation: Annotated[str, Argument(flags=("--operation",))]
    arguments: Annotated[
        dict[str, Any],
        Argument(
            flags=("--arguments",)
        ),
    ] = dataclasses.field(default_factory=dict)
    confirmed: Annotated[bool, Argument(flags=("--confirmed",))] = (
        False
    )

    def apply(self, ctx: Comms) -> CliResult:
        declaration = CliCommand.member_named(self.operation)
        return declaration.execute_target(
            ctx, declaration.selected_targets(CommandTarget.arguments(self.target, self.project)), self.arguments,
            confirmed=self.confirmed, channel=self.channel, project=self.project)


@dataclass(frozen=True, kw_only=True)
class StartCliCommand(NativeOwnerCommand, StartOwnerControl, ThreadLifecycleCliCommand):
    help = 'Start thread'

    def apply(self, ctx: Comms) -> OwnerStartResult:
        return ctx.owners.start(self.name)


@dataclass(frozen=True, kw_only=True)
class ThreadTagsCliCommand(NamedCliCommand, declared_name="thread-tags"):
    help = "Edit thread tags"
    tags: Annotated[
        frozenset[str],
        Normalize(_tags),
        EditorFormat(_tag_text),
        Argument(
            flags=("--tags",), options={"help": "Complete tags, separated by commas"}
        ),
    ]

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        return (cls(name=thread.name, tags=thread.tags),)

    def apply(self, ctx: Comms) -> ThreadTagsResult:
        thread = ctx.channels.replace_tags(self.name, self.tags)
        return ThreadTagsResult(thread.name, thread.tags)


@dataclass(frozen=True, kw_only=True)
class ExactTagCliCommand(NamedCliCommand):
    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name)

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return (cls.for_tag(resolved.channel.name.removeprefix('#')),) if resolved.channel.exact else ()


@dataclass(frozen=True, kw_only=True)
class RenameTagCliCommand(ExactTagCliCommand, declared_name="rename-tag"):
    help = "Rename channel tag"
    new_name: Annotated[
        str, Argument(flags=("--to",), options={"help": "New tag name"})
    ]

    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name, new_name='')

    def apply(self, ctx: Comms) -> TagRenamedResult:
        ctx.channels.rename_tag(self.name, self.new_name)
        return TagRenamedResult(self.name, self.new_name)


@dataclass(frozen=True, kw_only=True)
class DeleteTagCliCommand(MultipleTargets, ExactTagCliCommand, declared_name="delete-tag"):
    help = "Remove tag, archive tagged threads, or delete tagged threads"
    disposition: Annotated[
        TagDisposition,
        Editor(ChoiceTargetField),
        Argument(
            flags=("--disposition",), options={"help": "Choose what happens to tagged threads"}
        ),
    ] = dataclasses.field(default_factory=KeepThreadsTagDisposition)
    confirmed: Annotated[
        bool, TargetBound(), Argument(flags=("--confirmed",))
    ] = False

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
class ArchiveChannelCliCommand(MultipleTargets, NamedCliCommand, declared_name="archive-channel"):
    help = "Archive channel"
    archived: Annotated[
        bool, TargetBound(), Argument(flags=("--archived",))
    ] = True

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
class RestoreChannelCliCommand(ArchiveChannelCliCommand, declared_name="restore-channel"):
    help = "Restore archived channel"
    archived: Annotated[
        bool, TargetBound(), Argument(flags=("--archived",))
    ] = False

    @classmethod
    def catalog_declaration(cls):
        return cls


@dataclass(frozen=True, kw_only=True)
class DeleteViewCliCommand(NamedCliCommand, declared_name="delete-view"):
    help = "Delete saved view"

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return resolved.channel.delete_view_bindings(cls)

    def confirmation(self):
        return f"Delete saved view #{self.name}? Thread tags and messages are preserved."

    def apply(self, ctx: Comms) -> ViewDeletedResult:
        ctx.channels.delete_saved_view(self.name)
        return ViewDeletedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class PinChannelCliCommand(MultipleTargets, NamedCliCommand, declared_name="pin-channel"):
    help = "Set channel pin"
    pinned: Annotated[
        bool, TargetBound(), Argument(flags=("--pinned",))
    ] = False

    @classmethod
    def catalog_declaration(cls):
        return PinThreadCliCommand

    @classmethod
    def channel_bindings(cls, comms, resolved: ResolvedChannel, *, snapshot, catalog):
        return (cls(name=resolved.channel.name, pinned=not resolved.channel.pinned),) if resolved.channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_pinned(self.name, self.pinned)


@dataclass(frozen=True, kw_only=True)
class ChannelActivityCliCommand(NamedCliCommand, declared_name="channel-activity"):
    help = "Toggle member activity"
    enabled: Annotated[
        bool, TargetBound(), Argument(flags=("--enabled",))
    ] = False

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
                          self.confirmed, self.channel, self.project).apply(ctx)


@dataclass(frozen=True, kw_only=True)
class ReadTargetCliCommand(MultipleTargets, CliCommand, declared_name="read-target"):
    help = "Mark view read"
    target: Annotated[
        str, TargetBound(), Argument(flags=("--target",))
    ]
    worktree: Annotated[
        str, TargetBound(), Argument(flags=("--worktree",))
    ] = dataclasses.field(default_factory=os.getcwd)

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
class PinThreadCliCommand(MultipleTargets, NamedCliCommand, declared_name="pin-thread"):
    help = "Toggle thread pin in channel"
    channel: Annotated[
        str, TargetBound(), Argument(flags=("--channel",))
    ]
    pinned: Annotated[
        bool, TargetBound(), Argument(flags=("--pinned",))
    ] = False

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
        marker = WireLog(WireLog.path_in(ctx.root)).private_marker_unlocked()
        route = ActiveRoute(ctx.root, marker.root_id)
        return RouteReplacedResult(str(ctx.root), str(replace_unreadable_active_route(route)))
