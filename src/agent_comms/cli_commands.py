"""CLI commands own their options, boundary decoding and operation bodies.

The parser is a projection of dataclass fields; DeclaredFamily is the sole
command catalog. Existing CLI spellings belong to the store_files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import time
import types
from collections.abc import Callable
from dataclasses import MISSING, Field, asdict, dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Self, get_args, get_origin, get_type_hints

from .activity import ActivityState
from .channels import Channel
from .catalog_document import CatalogDocument
from .registry_document import RegistrySnapshot
from .child_process import ProcessIdentity
from .command import Command
from .field_codec import FieldCodec
from .comms import Comms
from .declared_family import DeclaredFamily
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
from .importing import ImportFormat, ImportLimits
from .turn_context import ContextSegment
from .working_memory_labels import ClassifierVersion, ModelLabel, QuestionVersion
from .working_memory_questions import SpanAnswer
from .messages import MessageType
from .message_reference import MessageReference
from .thread_management import ForkSpec
from .thread_execution import ThreadExecution, ExternalThreadExecution
from .owner_lifecycle import OwnerStartResult
from .channel_management import TagDisposition, KeepThreadsTagDisposition, TagChangeResult


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d|w)", value.strip().lower())
    if match is None:
        raise argparse.ArgumentTypeError("duration must look like 30s, 15m, 24h, 7d, or 2w")
    amount = float(match.group(1))
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


def _json_object(value: str) -> dict[str, Any]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("invoke --arguments must be a JSON object")
    return result


def _tags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def _tag_text(value: frozenset[str] | None) -> str:
    return ', '.join(sorted(value or ()))


def _shell_words(value: str | None) -> list[str] | None:
    return shlex.split(value) if value is not None else None


def _message_reference(value: str) -> dict[str, object]:
    """CLI spelling decodes once into the original wire reference."""
    sequence, message_id = value.split(":", 1)
    if int(sequence) <= 0 or not message_id:
        raise ValueError("Source must be a committed sequence:message_id reference")
    return {"seq": int(sequence), "message_id": message_id}


def option(
    *flags: str,
    default: Any = MISSING,
    default_factory: Any = MISSING,
    group: str | None = None,
    normalize: Callable[..., Any] | None = None,
    wire_name: str | None = None,
    parser_default: Any = MISSING,
    parser_default_factory: Any = MISSING,
    multiline: bool = False,
    target_bound: bool = False,
    editor_format: Callable[[Any], str] | None = None,
    **parser_options: Any,
) -> Any:
    """One field owns both its CLI projection and JSON boundary conversion."""
    metadata = {
        "multiline": multiline,
        "target_bound": target_bound,
        "editor_format": editor_format,
        "flags": flags,
        "parser_options": parser_options,
        "group": group,
        "normalize": normalize,
        "parser_default": parser_default,
        "parser_default_factory": parser_default_factory,
    }
    if wire_name is not None:
        metadata["wire_name"] = wire_name
    return field(default=default, default_factory=default_factory, metadata=metadata)


@dataclass(frozen=True)
class TargetField:
    """The original command field owns parsing, editor hints and requiredness."""
    declaration: Field
    annotation: object
    value: object

    @property
    def name(self) -> str:
        return self.declaration.metadata.get('wire_name', self.declaration.name)

    @property
    def multiline(self) -> bool:
        return self.declaration.metadata['multiline']

    @property
    def description(self) -> str:
        return self.declaration.metadata['parser_options'].get(
            'help', self.name.replace('_', ' ').capitalize())

    @property
    def editor_default(self) -> str:
        formatter = self.declaration.metadata['editor_format']
        if formatter is not None:
            return formatter(self.value)
        if self.choices:
            return FieldCodec.encode(type(self.value))
        return ('' if self.value is None else self.value if isinstance(self.value, str)
                else json.dumps(FieldCodec.encode(self.value)))

    @property
    def choices(self) -> tuple[tuple[str, str], ...]:
        if isinstance(self.annotation, type) and issubclass(self.annotation, DeclaredFamily):
            return tuple((member.label, FieldCodec.encode(member))
                         for member in self.annotation.members_with(self.annotation))
        return ()

    @property
    def required(self) -> bool:
        return self.declaration.default is MISSING and self.declaration.default_factory is MISSING

    def json_schema(self) -> dict[str, object]:
        return {**FieldCodec.value_schema(self.annotation), 'multiline': self.multiline,
                'description': self.description, 'editor_default': self.editor_default}


@dataclass(frozen=True)
class TargetAction:
    """A selection projects original bound commands, never a permission copy."""
    bound: tuple[CliCommand, ...]
    targets: tuple[str, ...]

    @property
    def declaration(self) -> type[CliCommand]:
        member = type(self.bound[0])
        return member.catalog_declaration() if len(self.targets) > 1 else member

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        command = self.bound[0]
        hints = get_type_hints(type(command))
        return tuple(TargetField(item, hints[item.name], getattr(command, item.name))
                     for item in fields(command) if not item.metadata['target_bound'])

    @property
    def label(self) -> str:
        return self.declaration.help

    @property
    def confirmation(self) -> str:
        return '\n'.join(dict.fromkeys(warning for command in self.bound
                                      if (warning := command.confirmation())))

    def edited(self, arguments: dict[str, str]) -> TargetAction:
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


@dataclass(frozen=True)
class TargetCompleted(TargetOutcome):
    target: str
    command: CliCommand
    result: Any
    successful: ClassVar[bool] = True

    def reconnect_targets(self) -> tuple[str, ...]:
        return type(self.command).reconnect_targets(self.result)


@dataclass(frozen=True)
class TargetFailed(TargetOutcome):
    target: str
    error_type: str
    error: str


@dataclass(frozen=True)
class TargetBatchResult:
    outcomes: tuple[TargetCompleted | TargetFailed, ...]

    @property
    def successful(self) -> bool:
        return all(outcome.successful for outcome in self.outcomes)

    def reconnect_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for outcome in self.outcomes
                                   for name in outcome.reconnect_targets()))


@dataclass(frozen=True)
class TargetEdit(Command):
    """Accepted editor values use the original declaration and fresh binding."""
    declaration: type[CliCommand]
    target: str | tuple[str, ...]
    arguments: dict[str, str]
    confirmed: bool = False
    channel: str | dict[str, str | None] | None = None

    def apply(self, ctx: Comms) -> object:
        arguments = self.declaration.editor_arguments(self.arguments)
        return self.declaration.execute_target(ctx, self.target, arguments,
                                              confirmed=self.confirmed, channel=self.channel)


@dataclass(frozen=True)
class ThreadStoppedResult:
    stopped: str


@dataclass(frozen=True)
class ThreadArchivedResult:
    archived: str


@dataclass(frozen=True)
class ThreadForkedResult:
    forked: str
    pid: int | None


@dataclass(frozen=True)
class ThreadTagsResult:
    thread: str
    tags: frozenset[str]


@dataclass(frozen=True)
class TagRenamedResult:
    renamed: str
    name: str


@dataclass(frozen=True)
class ViewDeletedResult:
    deleted_view: str


@dataclass(frozen=True)
class TargetReadResult:
    read: str


@dataclass(frozen=True)
class ThreadPinnedResult:
    thread: str
    pinned: bool


@dataclass(frozen=True, kw_only=True)
class CliCommand(DeclaredFamily, Command, affix="CliCommand"):
    help: ClassVar[str]
    multiple_targets: ClassVar[bool] = False

    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        """Concrete thread/channel variants declare their shared menu operation."""
        return cls

    @staticmethod
    def selected_targets(target: str | tuple[str, ...]) -> tuple[str, ...]:
        selected = (target,) if isinstance(target, str) else target
        if not selected or not all(isinstance(name, str) and name for name in selected):
            raise ValueError('Select at least one named target')
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def selection_channel(channel: str | dict[str, str | None] | None,
                          target: str) -> str | None:
        """Thread pins retain each selected row's original channel context."""
        return channel.get(target) if isinstance(channel, dict) else channel

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None) -> tuple[Self, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms, channel, *, snapshot=None) -> tuple[Self, ...]:
        return ()

    @classmethod
    def member_bindings(cls, comms, channel, *, snapshot=None) -> tuple[Self, ...]:
        """Use original roster membership and each command's thread eligibility."""
        from .presentation import ThreadView
        if snapshot is None:
            snapshot = comms.registry.snapshot()
        return tuple(bound for thread in snapshot.threads.values()
                     if channel.matches(thread.tags) and ThreadView.visible(
                         thread, snapshot, show_stopped=True, show_archived=False)
                     for bound in cls.thread_bindings(
                         comms, thread, snapshot.status(thread.name), channel.name))

    @classmethod
    def bindings(cls, comms: Comms, target: str, channel: str | None = None,
                 *, snapshot: RegistrySnapshot | None = None,
                 catalog: CatalogDocument | None = None) -> tuple[Self, ...]:
        from .channel_targets import is_channel_target
        if is_channel_target(target):
            if catalog is None:
                catalog = comms.channels.catalog.read()
            return cls.channel_bindings(comms, catalog.resolve(target), snapshot=snapshot)
        if snapshot is None:
            snapshot = comms.registry.snapshot()
        thread = snapshot.require(target)
        return cls.thread_bindings(comms, thread, snapshot.status(thread.name), channel,
                                   catalog=catalog)

    @classmethod
    def target_catalog(cls, comms: Comms, target: str | tuple[str, ...],
                       channel: str | dict[str, str | None] | None = None,
                       *, project: str) -> tuple[TargetAction, ...]:
        selected = cls.selected_targets(target)
        from .channel_targets import is_channel_target
        # A catalog is one observation. Declarations borrow its original store
        # resources; execution still acquires and rebinds current state.
        snapshot = comms.registry.snapshot()
        # View actors (including an implicit project actor) need not declare a
        # thread. Discovery has no thread actions for absent members; execution
        # still requires its original current binding. Resolve aliases through
        # the acquired namespace rather than reserving a special actor spelling.
        available_targets = tuple(name for name in selected
                                  if is_channel_target(name)
                                  or snapshot.canonical_name(name) in snapshot.threads)
        catalog = (comms.channels.catalog.read()
                   if channel or any(is_channel_target(name) for name in selected) else None)
        grouped: dict[type[CliCommand], list[CliCommand]] = {}
        for member in cls.members_with(cls):
            if len(selected) > 1 and not member.multiple_targets:
                continue
            for name in available_targets:
                try:
                    available = member.bindings(
                        comms, name, cls.selection_channel(channel, name),
                        snapshot=snapshot, catalog=catalog)
                except ValueError:
                    if len(selected) == 1:
                        raise
                    continue
                for command in available:
                    declaration = member.catalog_declaration() if len(selected) > 1 else member
                    commands = grouped.setdefault(declaration, [])
                    bound = command.for_editor(comms, name, project)
                    if bound not in commands:
                        commands.append(bound)
        return tuple(TargetAction(tuple(commands), selected) for commands in grouped.values())

    def for_editor(self, comms: Comms, target: str, project: str) -> Self:
        return self

    def describe(self, comms: Comms, target: str, project: str) -> TargetAction:
        bound = self.for_editor(comms, target, project)
        return TargetAction((bound,), (target,))

    def encode_result(self, result: object) -> object:
        return FieldCodec.encode(result)

    def result_exit_code(self, result: object) -> int:
        return 0

    @classmethod
    def reconnect_targets(cls, result) -> tuple[str, ...]:
        return result.reconnect_targets() if isinstance(result, TargetBatchResult) else ()

    @classmethod
    def editor_arguments(cls, values: dict[str, str]) -> dict[str, object]:
        hints = get_type_hints(cls)
        declared = {item.metadata.get('wire_name', item.name): item for item in fields(cls)}
        result = {}
        for key, text in values.items():
            item = declared[key]
            annotation = hints[item.name]
            normalize = item.metadata['normalize']
            if not text and type(None) in get_args(annotation):
                result[key] = None
            elif normalize is not None:
                result[key] = normalize(text)
            elif isinstance(annotation, type) and issubclass(annotation, DeclaredFamily):
                result[key] = {'kind': text}
            elif annotation is str or str in get_args(annotation):
                result[key] = text
            else:
                result[key] = json.loads(text)
        return result

    def confirmation(self) -> str:
        return ''

    def with_confirmation(self, confirmed: bool) -> Self:
        if self.confirmation() and not confirmed:
            raise ValueError(self.confirmation())
        return self

    def edited(self, arguments: dict[str, object]) -> Self:
        captured = {declared.metadata.get('wire_name', declared.name): getattr(self, declared.name)
                    for declared in fields(self) if declared.metadata['target_bound']}
        if captured.keys() & arguments.keys():
            raise ValueError('Target-bound parameters cannot be overridden')
        return type(self).from_payload({'kind': FieldCodec.encode(type(self)),
                                       **FieldCodec.encode(captured), **arguments})

    @classmethod
    def execute_target(cls, comms: Comms, target: str | tuple[str, ...], arguments: dict[str, object],
                       *, confirmed: bool = False,
                       channel: str | dict[str, str | None] | None = None) -> object:
        selected = cls.selected_targets(target)
        if isinstance(target, str):
            bindings = cls.bindings(comms, target, cls.selection_channel(channel, target))
            if len(bindings) == 1:
                return bindings[0].edited(arguments).with_confirmation(confirmed).apply(comms)
            if not bindings:
                raise ValueError('This action is no longer available for the target')
        if not cls.multiple_targets:
            raise ValueError('This action requires a single target')
        declarations = (tuple(member for member in CliCommand.members_with(CliCommand)
                              if member.catalog_declaration() is cls)
                        if cls.catalog_declaration() is cls else (cls,))
        outcomes: list[TargetCompleted | TargetFailed] = []
        planned: list[tuple[str, CliCommand]] = []
        for name in selected:
            try:
                bindings = tuple(command for member in declarations
                                 for command in member.bindings(
                                     comms, name, cls.selection_channel(channel, name)))
                if not bindings:
                    raise ValueError('This action is no longer available for the target')
            except Exception as error:
                outcomes.append(TargetFailed(name, type(error).__name__, str(error)))
                continue
            for command in bindings:
                if not any(command == previous for _, previous in planned):
                    planned.append((name, command))
        # Parameters and every existing warning are admitted before any write.
        edited = tuple(command.edited(arguments).with_confirmation(confirmed)
                       for _, command in planned)
        for (name, original), command in zip(planned, edited, strict=True):
            try:
                if original not in type(original).bindings(
                        comms, name, cls.selection_channel(channel, name)):
                    raise ValueError('The original target binding changed; refresh the selection')
                outcomes.append(TargetCompleted(name, command, command.apply(comms)))
            except Exception as error:
                outcomes.append(TargetFailed(name, type(error).__name__, str(error)))
        return TargetBatchResult(tuple(sorted(outcomes, key=lambda outcome: selected.index(outcome.target))))

    @classmethod
    def add_parser(cls, subparsers: Any) -> None:
        parser = subparsers.add_parser(FieldCodec.encode(cls), help=cls.help)
        groups: dict[str, Any] = {}
        hints = get_type_hints(cls)
        for declared in fields(cls):
            metadata = declared.metadata
            group = metadata["group"]
            owner = parser
            if group is not None:
                if group not in groups:
                    groups[group] = parser.add_mutually_exclusive_group(required=True)
                owner = groups[group]
            options = dict(metadata["parser_options"])
            annotation = hints[declared.name]
            scalar = (
                get_args(annotation)[0] if get_origin(annotation) is types.UnionType else annotation
            )
            if scalar is bool:
                options.setdefault("action", "store_true")
            elif scalar in (int, float):
                options.setdefault("type", scalar)
            elif isinstance(scalar, type) and issubclass(scalar, Enum):
                options["choices"] = [kind.value for kind in scalar]
            elif isinstance(scalar, type) and issubclass(scalar, DeclaredFamily):
                options["choices"] = scalar.names()
            if metadata["parser_default"] is not MISSING:
                options["default"] = metadata["parser_default"]
            elif metadata["parser_default_factory"] is not MISSING:
                options["default"] = metadata["parser_default_factory"]()
            elif declared.default is not MISSING:
                options["default"] = declared.default
            elif declared.default_factory is not MISSING:
                options["default"] = declared.default_factory()
            else:
                options["required"] = True
            if isinstance(options.get("default"), DeclaredFamily):
                options["default"] = options["default"].declared_name
            if any(flag.startswith("-") for flag in metadata["flags"]):
                options["dest"] = metadata.get("wire_name", declared.name)
            else:
                options.pop("required", None)
            owner.add_argument(*metadata["flags"], **options)

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> Self:
        member = cls.decode(args.command)
        payload = {"kind": member.declared_name}
        hints = get_type_hints(member)
        for declared in fields(member):
            key = declared.metadata.get("wire_name", declared.name)
            value = getattr(args, key)
            normalize = declared.metadata["normalize"]
            if normalize is not None:
                value = normalize(value)
            elif isinstance(hints[declared.name], type) and issubclass(
                hints[declared.name], DeclaredFamily
            ):
                value = {"kind": value}
            payload[key] = value
        return member.from_payload(payload)


@dataclass(frozen=True, kw_only=True)
class ToolsCliCommand(CliCommand):
    help = "Emit the shared adapter tool catalog"

    def apply(self, ctx: Comms) -> Any:
        from .tools import tool_catalog

        return {"tools": tool_catalog()}


@dataclass(frozen=True, kw_only=True)
class RepairInputRoutingCliCommand(CliCommand, declared_name="repair-input-routing"):
    help = "Preview historical native-input attribution repair"
    persist: bool = option(
        "--apply", help="Persist verified input routing", default=False, wire_name="apply"
    )

    def apply(self, ctx: Comms) -> Any:
        return ctx.transcripts.repair_input_routing(dry_run=not self.persist)


@dataclass(frozen=True, kw_only=True)
class ImportThreadCliCommand(CliCommand, declared_name="import-thread"):
    help = "Import an OpenCode/Codex context snapshot"
    format: ImportFormat = option("--format")
    source: str = option("--source", help="OpenCode export JSON/database or Codex rollout JSONL")
    session_id: str | None = option(
        "--session-id", help="Required for OpenCode database imports", default=None
    )
    name: str = option("--name", help="New agent-comms thread name")
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

    def apply(self, ctx: Comms) -> Any:
        receipt = ctx.threads.import_thread(
            self.source,
            self.format,
            name=self.name,
            session_id=self.session_id,
            worktree=self.worktree,
            model=self.model,
            tags=self.tags,
            limits=ImportLimits(
                self.max_messages, self.max_characters, self.max_message_characters
            ),
        )
        return receipt.to_wire()


@dataclass(frozen=True, kw_only=True)
class InvokeCliCommand(CliCommand):
    help = "Invoke one declared adapter tool"
    tool: str = option("--tool")
    arguments: dict[str, Any] = option(
        "--arguments",
        help="Tool arguments as a JSON object",
        default_factory=dict,
        parser_default="{}",
        normalize=_json_object,
    )

    def apply(self, ctx: Comms) -> Any:
        from .tools import invoke_tool

        return invoke_tool(ctx, self.tool, self.arguments)


@dataclass(frozen=True, kw_only=True)
class SendCliCommand(CliCommand):
    help = "Send to a thread (DM), #channel, or #all"
    sender: str = option("--from")
    target: str = option("--to")
    body: str = option("--body")
    type: MessageType = option("--type", default=MessageType.INFO)

    def apply(self, ctx: Comms) -> Any:
        mid = ctx.messaging.send(self.sender, self.target, self.body, self.type)
        return {"id": mid}


@dataclass(frozen=True, kw_only=True)
class PinConstraintCliCommand(CliCommand, declared_name="pin-constraint"):
    help = "Pin a certified original human message for its recipient"
    thread: str = option("thread")
    source: MessageReference = option("--source", normalize=_message_reference,
                                      help="Original sequence:message_id")
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def original_source(self, ctx: Comms):
        return self.source

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.pin_user_constraint(self.thread, source, worktree=self.worktree)

    def apply(self, ctx: Comms) -> Any:
        from .field_codec import FieldCodec

        source = self.original_source(ctx)
        pin = self.pin_original(ctx, source)
        return {"pin": FieldCodec.encode(pin.reference), "source": FieldCodec.encode(source)}


@dataclass(frozen=True, kw_only=True)
class PinInputConstraintCliCommand(PinConstraintCliCommand, declared_name="pin-input-constraint"):
    help = "Pin an original human input for its recipient"
    source: str = option("--source", help="Original durable input key")

    def original_source(self, ctx: Comms):
        from .input_disposition import InputDispositions
        from .errors import RelationViolationError

        with InputDispositions(ctx.root / InputDispositions.filename).reading() as inputs:
            try:
                original = inputs.rows[self.source]
            except KeyError as error:
                raise RelationViolationError("Constraint lacks its original input") from error
            return original.context_provenance().require_human_input()

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.pin_input_constraint(self.thread, source, worktree=self.worktree)


@dataclass(frozen=True, kw_only=True)
class ApproveAnnotationsCliCommand(PinConstraintCliCommand, declared_name="approve-annotations"):
    help = "Approve a pinned external classifier using original human wording and an hourly request budget"
    per_hour: int = option("--per-hour", help="Maximum classifier requests in the rolling hour")
    segments: tuple[type[ContextSegment], ...] = option("--segments", normalize=json.loads,
        help="JSON list of original segment kinds permitted for disclosure")

    def pin_original(self, ctx: Comms, source):
        return ctx.messaging.approve_annotations(self.thread, source, worktree=self.worktree,
                                                per_hour=self.per_hour, segments=self.segments)


@dataclass(frozen=True, kw_only=True)
class CorrectAnnotationCliCommand(CliCommand, declared_name="correct-annotation"):
    help = "Correct one original stored model answer through the human identity owner"
    label: ModelLabel = option("--label", normalize=json.loads)
    answer: type[SpanAnswer] = option("--answer")
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def apply(self, ctx: Comms):
        from .coordinator import Coordination

        author = ctx.messaging.user_identity(self.worktree).incarnation
        snapshot = ctx.registry.snapshot()
        with Coordination(str(ctx.root / "coordination.sqlite3")) as store:
            return store.annotations.correct(self.label, self.answer, author, snapshot)


@dataclass(frozen=True, kw_only=True)
class AnnotationsCalibrationCliCommand(CliCommand, declared_name="annotations"):
    help = "Report human-reviewed accuracy and probability frequencies for exact annotation versions"
    operation: Literal["calibration"] = option("operation", help="calibration")
    question: QuestionVersion = option("--question", normalize=json.loads,
        help="JSON of the original question member and version digest")
    classifier: ClassifierVersion = option("--classifier", normalize=json.loads,
        help="JSON of the original classifier member and pinned release")

    def apply(self, ctx: Comms):
        from .working_memory_annotations import WorkingMemoryAnnotations

        return WorkingMemoryAnnotations.calibration(
            ctx.root / "coordination.sqlite3", self.question, self.classifier)

    def encode_result(self, result):
        return result.public_report()


@dataclass(frozen=True, kw_only=True)
class SupersedeConstraintCliCommand(CliCommand, declared_name="supersede-constraint"):
    help = "Publish a human's exact replacement for an authored constraint"
    thread: str = option("thread")
    source: MessageReference = option("--source", normalize=_message_reference)
    body: str = option("--body")
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def apply(self, ctx: Comms) -> Any:
        from .field_codec import FieldCodec
        from .task_sources import CorrectionTaskChange, UserTaskSupersession

        message = ctx.messaging.send_user_message(self.thread, self.body, worktree=self.worktree,
            task=UserTaskSupersession(CorrectionTaskChange(self.source)))
        return {"supersession": FieldCodec.encode(message.reference)}


@dataclass(frozen=True, kw_only=True)
class DropConstraintCliCommand(CliCommand, declared_name="drop-constraint"):
    help = "Retire a constraint by original reference, preserving its evidence"
    thread: str = option("thread")
    source: MessageReference = option("--source", normalize=_message_reference)
    worktree: str = option("--worktree", default_factory=os.getcwd)

    def apply(self, ctx: Comms) -> Any:
        from .field_codec import FieldCodec
        from .task_sources import CorrectionTaskChange, UserTaskDrop

        message = ctx.messaging.send_user_message(self.thread,
            f"Dropped constraint {self.source.seq}:{self.source.message_id}", worktree=self.worktree,
            task=UserTaskDrop(CorrectionTaskChange(self.source)))
        return {"drop": FieldCodec.encode(message.reference)}


@dataclass(frozen=True, kw_only=True)
class ExportRetainedCliCommand(CliCommand, declared_name="export-retained"):
    help = "Export current authored context from original certified sources"
    thread: str = option("thread")
    output: str = option("--output")
    overwrite: bool = option("--overwrite", default=False)

    def apply(self, ctx: Comms) -> Any:
        return ctx.bus.log.retained_context(self.thread, ctx.registry).export(
            self.output, overwrite=self.overwrite).to_wire()


@dataclass(frozen=True, kw_only=True)
class RetainedContextCliCommand(CliCommand, declared_name="retained-context"):
    help = "Inspect retained context and original provenance without native input"
    thread: str = option("thread")
    diff: bool = option("--diff", default=False, action="store_true",
                        help="Compare the last two original selected compaction source cuts")

    def apply(self, ctx: Comms) -> Any:
        from .compaction_boundary import CompactionBoundary
        from .input_disposition import InputDispositions

        boundary = CompactionBoundary(ctx.registry,
            InputDispositions(ctx.root / InputDispositions.filename))
        if self.diff:
            return dict(boundary.retained_history(ctx.registry.require(self.thread), diff=True),
                        input_supplied=False)
        return boundary.inspect(self.thread)


@dataclass(frozen=True, kw_only=True)
class InboxCliCommand(CliCommand):
    help = "Undelivered messages for a thread"
    thread: str = option("--thread")

    def apply(self, ctx: Comms) -> Any:
        return {"messages": [m.to_wire() for m in ctx.bus.inbox(self.thread)]}


@dataclass(frozen=True, kw_only=True)
class AckCliCommand(CliCommand):
    help = "Mark inbox delivered"
    thread: str = option("--thread")

    def apply(self, ctx: Comms) -> Any:
        return {"acknowledged": ctx.messaging.acknowledge(self.thread)}


@dataclass(frozen=True, kw_only=True)
class WhoCliCommand(CliCommand):
    help = "Presence: who is in the chat"

    def apply(self, ctx: Comms) -> Any:
        return {"who": list(ctx.views.who())}


@dataclass(frozen=True, kw_only=True)
class StatusCliCommand(CliCommand):
    help = "Live activity: what each thread is doing right now"

    def apply(self, ctx: Comms) -> Any:
        return {
            "status": [
                {
                    "thread": thread,
                    "state": activity.state.value,
                    "detail": activity.detail,
                    "ts": activity.timestamp,
                }
                for thread, activity in sorted(ctx.agents.all_activity().items())
            ]
        }


@dataclass(frozen=True, kw_only=True)
class HistoryCliCommand(CliCommand):
    help = "Full history of a DM, channel, or everything"
    with_thread: str | None = option("--with", help="DM with this thread", default=None)
    channel: str | None = option("--channel", help="Channel (#all, #tag)", default=None)
    everything: bool = option("--everything", help="Every message on the wire", default=False)
    me: str | None = option("--as", help="Your thread (for --with)", default=None)

    def apply(self, ctx: Comms) -> Any:
        selected = sum(bool(x) for x in (self.with_thread, self.channel, self.everything))
        if selected != 1:
            raise ValueError("history takes exactly one of --with, --channel, --everything")
        if self.with_thread:
            if not self.me:
                raise ValueError("history --with requires --as (your thread)")
            return {
                "dm": [
                    {**m.to_wire(), **m.display_metadata}
                    for m in ctx.views.dm_history(self.me, self.with_thread)
                ]
            }
        elif self.channel:
            return {
                "channel": self.channel,
                "messages": [
                    {**m.to_wire(), **m.display_metadata}
                    for m in ctx.views.channel_history(self.channel)
                ],
            }
        else:
            return {
                "everything": [
                    {**m.to_wire(), **m.display_metadata} for m in ctx.views.full_history()
                ]
            }


@dataclass(frozen=True, kw_only=True)
class ExportWireCliCommand(CliCommand, declared_name="export-wire"):
    help = "Export durable IRC/wire message history"
    output: str = option("--output", help="Destination file")
    format: SelectableWireExportFormat = option("--format", default=JsonlFormat())
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

    def apply(self, ctx: Comms) -> Any:
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
        export_receipt = ctx.views.export_wire(
            self.output,
            format=self.format,
            scope=scope,
            limit=export_limit,
            overwrite=self.overwrite,
            export_started_at=started_at,
        )
        return export_receipt.to_wire()


@dataclass(frozen=True, kw_only=True)
class ChannelsCliCommand(CliCommand):
    help = "Derived channel list"

    def apply(self, ctx: Comms) -> Any:
        return {"channels": list(ctx.channels.channels())}


@dataclass(frozen=True, kw_only=True)
class ThreadsCliCommand(CliCommand):
    help = "List threads"
    active_only: bool = option("--active-only", default=False)

    def apply(self, ctx: Comms) -> Any:
        return {"threads": list(ctx.views.list_threads(active_only=self.active_only))}


@dataclass(frozen=True, kw_only=True)
class ThreadCliCommand(CliCommand):
    help = "Detail for one thread"
    name: str = option("--name")
    no_pending: bool = option(
        "--no-pending", help="Skip the inbox count for fast metadata reads", default=False
    )

    def apply(self, ctx: Comms) -> Any:
        return ctx.views.thread_detail(self.name, include_pending=not self.no_pending)


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
    pid: int = option("--pid", default=0)
    execution: type[ThreadExecution] = option("--execution", default=ExternalThreadExecution,
                                            parser_default=FieldCodec.encode(ExternalThreadExecution))

    def apply(self, ctx: Comms) -> Any:
        from .threads import Thread

        thread = Thread(
            name=self.name,
            tags=self.tags,
            worktree=self.worktree,
            parent=self.parent,
            task=self.task,
            process_identity=ProcessIdentity.capture(self.pid) if self.pid > 0 else None,
            execution=self.execution,
        )
        ctx.registry.declare(thread)
        return {"registered": self.name}


@dataclass(frozen=True, kw_only=True)
class HeartbeatCliCommand(CliCommand):
    help = "Mark thread running"
    name: str = option("--name")

    def apply(self, ctx: Comms) -> Any:
        ctx.threads.heartbeat(self.name)
        return {"heartbeat": self.name}


@dataclass(frozen=True, kw_only=True)
class AttachSessionCliCommand(CliCommand, declared_name="attach-session"):
    help = "Attach a live Pi session to a registered thread"
    name: str = option("--name")
    session_file: str = option("--session-file")
    pid: int | None = option("--pid", default=None)

    def apply(self, ctx: Comms) -> Any:
        attached = ctx.threads.attach_session(
            ctx.registry.require(self.name), self.session_file, pid=self.pid
        )
        return {
            "attached": attached.name,
            "session_file": attached.session_file,
            "pid": attached.pid,
        }


@dataclass(frozen=True, kw_only=True)
class ActivityCliCommand(CliCommand):
    help = "Publish current thread activity"
    name: str = option("--name")
    state: ActivityState = option("--state")
    detail: str = option("--detail", default="")

    def apply(self, ctx: Comms) -> Any:
        ctx.agents.set_activity(self.name, self.state, self.detail)
        return {"activity": self.name, "state": self.state.value}


@dataclass(frozen=True, kw_only=True)
class StopCliCommand(CliCommand):
    help = "Stop process"
    multiple_targets = True
    name: str = option("--name", target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        from .tools import CommsStopTool
        return (cls(name=thread.name),) if CommsStopTool.available_for_thread(thread, status) else ()

    @classmethod
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return cls.member_bindings(comms, channel, snapshot=snapshot)

    def apply(self, ctx: Comms) -> ThreadStoppedResult:
        ctx.owners.stop(self.name)
        return ThreadStoppedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class RestartCliCommand(CliCommand):
    help = "Restart idle running owners to reload backend code"
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

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        from .errors import RelationViolationError

        try:
            thread.require_restart_owner(status)
            thread.require_idle()
        except RelationViolationError:
            return ()
        return (cls(name=thread.name),)

    def apply(self, ctx: Comms) -> Any:
        results = ctx.owners.restart_owners(
            None if self.all_ else [self.name], agent_bin=self.agent_bin, agent_args=self.agent_args
        )
        return {"restarted": [asdict(result) for result in results]}


@dataclass(frozen=True, kw_only=True)
class ReleaseCliCommand(CliCommand):
    help = "Voluntarily mark the calling thread stopped"
    name: str = option("--name")

    def apply(self, ctx: Comms) -> Any:
        ctx.owners.release(self.name)
        return {"released": self.name}


@dataclass(frozen=True, kw_only=True)
class ArchiveCliCommand(CliCommand):
    help = "Archive a stopped thread"
    multiple_targets = True
    name: str = option("--name", target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        from .tools import CommsArchiveTool
        return (cls(name=thread.name),) if CommsArchiveTool.available_for_thread(thread, status) else ()

    def apply(self, ctx: Comms) -> ThreadArchivedResult:
        ctx.threads.archive(self.name)
        return ThreadArchivedResult(self.name)




@dataclass(frozen=True, kw_only=True)
class RenameSelfCliCommand(CliCommand, declared_name="rename-self"):
    help = "Rename your own running thread"
    new_name: str = option("--to")

    def apply(self, ctx: Comms) -> Any:
        rename_result = ctx.threads.rename_self(self.new_name)
        return {
            "previous": rename_result.previous,
            "current": rename_result.current,
            "changed": rename_result.changed,
        }


@dataclass(frozen=True, kw_only=True)
class ForkCliCommand(CliCommand):
    help = "Fork a child pi thread"
    name: str = option("--name")
    parent: str = option("--parent", target_bound=True)
    task: str = option("--task", default="", multiline=True)
    tags: frozenset[str] | None = option(
        "--tags", default=None, editor_format=_tag_text, normalize=lambda value: _tags(value) if value is not None else None
    )
    prompt: str | None = option("--prompt", default=None, multiline=True)
    pi_bin: str = option(
        "--pi-bin", default_factory=lambda: os.environ.get("AGENT_COMMS_AGENT_BIN", "pi")
    )

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        from .tools import CommsForkTool
        return (cls(name='', parent=thread.name, tags=thread.tags),) if CommsForkTool.available_for_thread(thread, status) else ()

    def apply(self, ctx: Comms) -> ThreadForkedResult:
        spec = ForkSpec(
            name=self.name, parent=self.parent, task=self.task, tags=self.tags, prompt=self.prompt
        )
        child = ctx.threads.fork(spec, pi_bin=self.pi_bin)
        return ThreadForkedResult(child.name, child.pid)


@dataclass(frozen=True, kw_only=True)
class LedgerCliCommand(CliCommand):
    help = "Read or merge ledger state"
    merge_from: str | None = option("--merge-from", default=None)
    author: str | None = option("--author", default=None)

    def apply(self, ctx: Comms) -> Any:
        if self.merge_from is not None:
            if not self.author:
                raise ValueError("ledger merge requires --author")
            updates = json.loads(Path(self.merge_from).read_text())
            ctx.ledger.merge(updates, self.author)
            return {"merged": True}
        else:
            return ctx.ledger.read()


@dataclass(frozen=True, kw_only=True)
class PollCliCommand(CliCommand):
    help = "Status snapshot: thread, inbox, ledger, peers"
    thread: str | None = option("--thread", default=None)

    def apply(self, ctx: Comms) -> Any:
        return ctx.views.poll(self.thread)


@dataclass(frozen=True, kw_only=True)
class CompactionStatusCliCommand(CliCommand, declared_name="compaction-status"):
    help = "Inspect durable native compaction outcomes without replaying work"
    thread: str = option("--thread")

    def apply(self, ctx: Comms) -> Any:
        from functools import partial
        from .compaction_journal import CompactionJournal
        from .compaction_records import SelectedSummaryAttempt

        owner = ctx.registry.require(self.thread)
        try:
            session_file = owner.require_saved_session()
        except ValueError:
            return dict(thread=owner.name, attempts=[])
        attempts = CompactionJournal.observe_readonly(
            ctx.root / "compaction-commits.sqlite3",
            partial(SelectedSummaryAttempt.for_session, canonical=session_file),
            absent=(),
        )
        return dict(thread=owner.name, attempts=[
            dict(operation_id=row.operation_id, state=row.state)
            for row in attempts
        ])


@dataclass(frozen=True, kw_only=True)
class ContextCliCommand(CliCommand):
    help = "Inspect original context contributors without sending an input"
    thread: str = option("thread")
    turn: int | None = option("--turn", default=None, help="Original admitted turn generation")
    diff: bool = option("--diff", default=False, action="store_true")
    imported: bool = option("--imported", default=False, action="store_true",
                            help="Original imported instruction references, not current context")

    def apply(self, ctx: Comms) -> Any:
        from .importing import ImportedSessionMetadata

        if self.imported:
            if self.turn is not None or self.diff:
                raise ValueError("Imported source references are not recorded native turns")
            owner = ctx.registry.require(self.thread)
            return {"scope": "historical-imported-instructions; not current or recorded native context",
                    "sources": ImportedSessionMetadata.sources_for_owner(ctx.registry, owner)}
        if self.turn is not None or self.diff:
            manifests = ctx.bus.log.context_manifests(self.thread, ctx.registry)
            selected = tuple(
                manifest for manifest in manifests
                if self.turn is None or manifest.turn.matches_generation(self.turn)
            )
            if not selected:
                raise ValueError("No original context manifest exists for the requested turn")
            if self.diff:
                return selected[-1].changed_from_history(manifests)
            return {"manifests": selected,
                    "text_recorded": all(manifest.public_text_recorded for manifest in selected)}
        import asyncio
        from .private_nk_entrypoint import PrivateNkLaunch
        from .context_tokens import NativeTokenCounter
        from .field_codec import FieldCodec
        from .native_turn_context import NativeContextData
        from .runtime import RuntimeConnection, socket_path
        from .turn_context import NextContextTurn

        owner = ctx.registry.require(self.thread)
        launch = PrivateNkLaunch.from_environment(
            ctx.root, ctx.owners.restart_environment(os.environ)
        )
        if launch is None:
            raise ValueError("Context inspection requires this root's configured native package")
        counter = NativeTokenCounter(launch.native_package)
        connection = RuntimeConnection(ctx, owner.name, socket_path(ctx.root, owner.require_process().pid))

        async def inspect_native():
            try:
                payload = await connection.request("context")
                return FieldCodec.decode(NativeContextData, payload).require_session_file(
                    owner.require_saved_session()
                )
            finally:
                await connection.close()

        native = asyncio.run(inspect_native())
        context = native.contributor_context(owner, NextContextTurn())
        counts = counter.measure(tuple(segment.text() for segment in context.segments))
        native_context = native.for_turn(owner, NextContextTurn())
        return {
            "scope": "next-native-base-and-core-contributors; before future input and provider hooks",
            "input_supplied": False,
            "native_manifest": native_context.segments,
            "manifest": context.manifest(counts.counts, counter=counts.counter),
            "native_provider_context": native_context.render().provider,
            "native_source_spans": [
                {"segment": position, "sha256": segment.measured_manifest().sha256,
                 "spans": FieldCodec.encode(segment.source_ranges())}
                for position, segment in enumerate(native.segments)
            ],
            "segments": [
                dict(
                    kind=type(segment),
                    provenance=segment.provenance,
                    tokens=count,
                    text=segment.text(),
                )
                for segment, count in zip(context.segments, counts.counts, strict=True)
            ],
        }


@dataclass(frozen=True, kw_only=True)
class TargetActionsCliCommand(CliCommand, declared_name='target-actions'):
    project: str = option('--project', default_factory=os.getcwd)
    channel: str | None = option('--channel', default=None)
    help = 'List applicable operations and their declared parameters'
    target: str | tuple[str, ...] = option('--target', '--targets', nargs='+',
        normalize=lambda names: names[0] if len(names) == 1 else names)

    def apply(self, ctx: Comms) -> tuple[TargetAction, ...]:
        return CliCommand.target_catalog(ctx, self.target, self.channel, project=self.project)

    def encode_result(self, result: tuple[TargetAction, ...]) -> object:
        return {'actions': [action.encode() for action in result]}


@dataclass(frozen=True, kw_only=True)
class TargetActionCliCommand(CliCommand, declared_name='target-action'):
    channel: str | None = option('--channel', default=None)
    help = 'Execute a declared operation against its current target'
    target: str | tuple[str, ...] = option('--target', '--targets', nargs='+',
        normalize=lambda names: names[0] if len(names) == 1 else names)
    operation: str = option('--operation')
    arguments: dict[str, Any] = option('--arguments', default_factory=dict,
                                     parser_default='{}', normalize=_json_object)
    confirmed: bool = option('--confirmed', default=False)

    def apply(self, ctx: Comms) -> object:
        return CliCommand.decode(self.operation).execute_target(
            ctx, self.target, self.arguments, confirmed=self.confirmed, channel=self.channel)

    def result_exit_code(self, result: object) -> int:
        return int(isinstance(result, TargetBatchResult) and not result.successful)


@dataclass(frozen=True, kw_only=True)
class StartCliCommand(CliCommand):
    help = 'Start thread'
    multiple_targets = True
    name: str = option('--name', target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        from .tools import CommsStartTool
        return (cls(name=thread.name),) if CommsStartTool.available_for_thread(thread, status) else ()

    @classmethod
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return cls.member_bindings(comms, channel, snapshot=snapshot)

    @classmethod
    def reconnect_targets(cls, result: OwnerStartResult | TargetBatchResult) -> tuple[str, ...]:
        if isinstance(result, TargetBatchResult):
            return super().reconnect_targets(result)
        return (result.thread,) if result.launched else ()

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
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return (cls.for_tag(channel.name.removeprefix('#')),) if channel.exact else ()


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
    disposition: TagDisposition = option('--disposition', default_factory=KeepThreadsTagDisposition,
                                        help='Choose what happens to tagged threads')
    confirmed: bool = option('--confirmed', default=False, target_bound=True)

    def confirmation(self):
        return self.disposition.confirmation(self.name)

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
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return (cls(name=channel.name),) if channel.can_set_archived(cls.archived) else ()

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

    @classmethod
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return (cls(name=channel.name),) if channel.can_set_archived(cls.archived) else ()


@dataclass(frozen=True, kw_only=True)
class DeleteViewCliCommand(CliCommand, declared_name='delete-view'):
    help = 'Delete saved view'
    name: str = option('--name', target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return (cls(name=channel.view.name),) if channel.view is not None else ()

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
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return (cls(name=channel.name, pinned=not channel.pinned),) if channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_pinned(self.name, self.pinned)


@dataclass(frozen=True, kw_only=True)
class ChannelActivityCliCommand(CliCommand, declared_name='channel-activity'):
    help = 'Toggle member activity'
    name: str = option('--name', target_bound=True)
    enabled: bool = option('--enabled', default=False, target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return (cls(name=channel.name, enabled=not channel.any_mode),) if channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_any_mode(self.name, self.enabled)


@dataclass(frozen=True, kw_only=True)
class TargetEditCliCommand(TargetActionCliCommand, declared_name='target-edit'):
    help = 'Execute a declared operation using its original CLI field parsers'

    def apply(self, ctx: Comms) -> object:
        return TargetEdit(CliCommand.decode(self.operation), self.target, self.arguments,
                          self.confirmed, self.channel).apply(ctx)


@dataclass(frozen=True, kw_only=True)
class ReadTargetCliCommand(CliCommand, declared_name='read-target'):
    help = 'Mark view read'
    multiple_targets = True
    target: str = option('--target', target_bound=True)
    worktree: str = option('--worktree', default_factory=os.getcwd)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        return (cls(target=thread.name),)

    @classmethod
    def channel_bindings(cls, comms, channel, *, snapshot=None):
        return (cls(target=channel.name),)

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
    def thread_bindings(cls, comms, thread, status, channel=None, *, catalog=None):
        if channel is None:
            return ()
        document = catalog if catalog is not None else comms.channels.catalog.read()
        if not document.resolve(channel).matches(thread.tags):
            return ()
        return (cls(name=thread.name, channel=channel,
                    pinned=thread.name not in document.pinned_threads(channel)),)

    def apply(self, ctx: Comms) -> ThreadPinnedResult:
        ctx.channels.set_thread_pinned(self.channel, self.name, self.pinned)
        return ThreadPinnedResult(self.name, self.pinned)
