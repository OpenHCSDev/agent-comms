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
from typing import Any, ClassVar, Self, get_args, get_origin, get_type_hints

from .activity import ActivityState
from .channels import Channel
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
            return tuple((member.label, member.declared_name)
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
    """One command instance carries its target binding and editable declarations."""
    bound: CliCommand
    editable_fields: tuple[TargetField, ...]

    @property
    def declaration(self) -> type[CliCommand]:
        return type(self.bound)

    @property
    def label(self) -> str:
        return self.declaration.help

    @property
    def confirmation(self) -> str:
        return self.bound.confirmation()

    def edited(self, arguments: dict[str, str]) -> CliCommand:
        return self.bound.edited(self.declaration.editor_arguments(arguments))

    def encode(self) -> dict[str, object]:
        """Only the CLI boundary requests the external catalog JSON shape."""
        return {'command': self.declaration.declared_name, 'label': self.label,
                'parameters': {'type': 'object', 'additionalProperties': False,
                    'properties': {item.name: item.json_schema() for item in self.editable_fields},
                    'required': [item.name for item in self.editable_fields if item.required]},
                'confirmation': self.confirmation}


@dataclass(frozen=True)
class TargetEdit(Command):
    """Accepted editor values use the original declaration and fresh binding."""
    declaration: type[CliCommand]
    target: str
    arguments: dict[str, str]
    confirmed: bool = False
    channel: str | None = None

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

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None) -> tuple[Self, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms, channel) -> tuple[Self, ...]:
        return ()

    @classmethod
    def bindings(cls, comms: Comms, target: str, channel: str | None = None) -> tuple[Self, ...]:
        from .channel_targets import is_channel_target
        if is_channel_target(target):
            return cls.channel_bindings(comms, comms.channels.catalog.read().resolve(target))
        snapshot = comms.registry.snapshot()
        thread = snapshot.require(target)
        return cls.thread_bindings(comms, thread, snapshot.status(thread.name), channel)

    @classmethod
    def target_catalog(cls, comms: Comms, target: str, channel: str | None = None, *, project: str) -> tuple[TargetAction, ...]:
        from .channel_targets import is_channel_target
        if is_channel_target(target):
            view = comms.channels.catalog.read().resolve(target)
            bindings = ((member, member.channel_bindings(comms, view))
                        for member in cls.members_with(cls))
        else:
            snapshot = comms.registry.snapshot()
            thread = snapshot.require(target)
            status = snapshot.status(thread.name)
            bindings = ((member, member.thread_bindings(comms, thread,
                        status, channel))
                        for member in cls.members_with(cls))
        return tuple(bound.describe(comms, target, project)
                     for member, available in bindings for bound in available)

    def for_editor(self, comms: Comms, target: str, project: str) -> Self:
        return self

    def describe(self, comms: Comms, target: str, project: str) -> TargetAction:
        bound = self.for_editor(comms, target, project)
        hints = get_type_hints(type(bound))
        return TargetAction(bound, tuple(
            TargetField(declared, hints[declared.name], getattr(bound, declared.name))
            for declared in fields(bound) if not declared.metadata['target_bound']))

    def encode_result(self, result: object) -> object:
        return FieldCodec.encode(result)

    @classmethod
    def reconnect_targets(cls, result) -> tuple[str, ...]:
        return ()

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
        return type(self).from_payload({'kind': self.declared_name,
                                       **FieldCodec.encode(captured), **arguments})

    @classmethod
    def execute_target(cls, comms: Comms, target: str, arguments: dict[str, object],
                       *, confirmed: bool = False, channel: str | None = None) -> object:
        bindings = cls.bindings(comms, target, channel)
        if len(bindings) != 1:
            raise ValueError('This action is no longer available for the target')
        bound, = bindings
        edited = bound.edited(arguments)
        return edited.with_confirmation(confirmed).apply(comms)

    @classmethod
    def add_parser(cls, subparsers: Any) -> None:
        parser = subparsers.add_parser(cls.declared_name, help=cls.help)
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
                                            parser_default=ExternalThreadExecution.declared_name)

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
    help = "Mark thread stopped"
    name: str = option("--name", target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        from .tools import CommsStopTool
        return (cls(name=thread.name),) if CommsStopTool.available_for_thread(thread, status) else ()

    def apply(self, ctx: Comms) -> ThreadStoppedResult:
        ctx.owners.stop(self.name)
        return ThreadStoppedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class RestartCliCommand(CliCommand):
    help = "Restart idle running owners to reload backend code"
    name: str | None = option(
        "--name", help="One running thread (aliases supported)", group="scope", default=None
    )
    all_: bool = option(
        "--all",
        help="All running owners; excludes stopped threads",
        group="scope",
        default=False,
        wire_name="all",
    )
    agent_bin: str | None = option("--agent-bin", default=None)
    agent_args: list[str] | None = option(
        "--agent-args",
        default=None,
        normalize=_shell_words,
    )

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
    name: str = option("--name", target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
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
    def thread_bindings(cls, comms, thread, status, channel=None):
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
        from .compaction_boundary import CompactionBoundary
        from .compaction_records import SelectedSummaryAttempt
        from .input_disposition import InputDispositions

        owner = ctx.registry.require(self.thread)
        history = CompactionBoundary(ctx.registry,
            InputDispositions(ctx.root / InputDispositions.filename)).retained_history(owner)
        return dict(thread=owner.name, attempts=[
            dict(operation_id=row["operation_id"], state=row["state"])
            for row in history.get("tables", {}).get(SelectedSummaryAttempt.declared_name, ())
        ])


@dataclass(frozen=True, kw_only=True)
class ContextCliCommand(CliCommand):
    help = "Inspect original context contributors without sending an input"
    thread: str = option("thread")
    turn: int | None = option("--turn", default=None, help="Original admitted turn generation")
    diff: bool = option("--diff", default=False, action="store_true")

    def apply(self, ctx: Comms) -> Any:
        import asyncio
        from .private_nk_entrypoint import PrivateNkLaunch
        from .context_tokens import NativeTokenCounter
        from .field_codec import FieldCodec
        from .native_turn_context import NativeContextData
        from .runtime import RuntimeConnection, socket_path
        from .turn_context import NextContextTurn

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
            return {"manifests": FieldCodec.encode(selected),
                    "text_recorded": all(manifest.public_text_recorded for manifest in selected)}
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
            "native_manifest": FieldCodec.encode(native_context.segments),
            "manifest": FieldCodec.encode(context.manifest(counts.counts, counter=counts.counter)),
            "native_provider_context": native_context.render().provider,
            "segments": [
                dict(
                    kind=segment.declared_name,
                    provenance=FieldCodec.encode(segment.provenance),
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
    target: str = option('--target')

    def apply(self, ctx: Comms) -> tuple[TargetAction, ...]:
        return CliCommand.target_catalog(ctx, self.target, self.channel, project=self.project)

    def encode_result(self, result: tuple[TargetAction, ...]) -> object:
        return {'actions': [action.encode() for action in result]}


@dataclass(frozen=True, kw_only=True)
class TargetActionCliCommand(CliCommand, declared_name='target-action'):
    channel: str | None = option('--channel', default=None)
    help = 'Execute a declared operation against its current target'
    target: str = option('--target')
    operation: str = option('--operation')
    arguments: dict[str, Any] = option('--arguments', default_factory=dict,
                                     parser_default='{}', normalize=_json_object)
    confirmed: bool = option('--confirmed', default=False)

    def apply(self, ctx: Comms) -> object:
        return CliCommand.decode(self.operation).execute_target(
            ctx, self.target, self.arguments, confirmed=self.confirmed, channel=self.channel)


@dataclass(frozen=True, kw_only=True)
class StartCliCommand(CliCommand):
    help = 'Start thread'
    name: str = option('--name', target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        from .tools import CommsStartTool
        return (cls(name=thread.name),) if CommsStartTool.available_for_thread(thread, status) else ()

    @classmethod
    def reconnect_targets(cls, result: OwnerStartResult) -> tuple[str, ...]:
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
    def thread_bindings(cls, comms, thread, status, channel=None):
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
    def channel_bindings(cls, comms, channel):
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
    name: str = option('--name', target_bound=True)
    archived: bool = option('--archived', default=True, target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, channel):
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
    def channel_bindings(cls, comms, channel):
        return (cls(name=channel.name),) if channel.can_set_archived(cls.archived) else ()


@dataclass(frozen=True, kw_only=True)
class DeleteViewCliCommand(CliCommand, declared_name='delete-view'):
    help = 'Delete saved view'
    name: str = option('--name', target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, channel):
        return (cls(name=channel.view.name),) if channel.view is not None else ()

    def confirmation(self):
        return f"Delete saved view #{self.name}? Thread tags and messages are preserved."

    def apply(self, ctx: Comms) -> ViewDeletedResult:
        ctx.channels.delete_saved_view(self.name)
        return ViewDeletedResult(self.name)


@dataclass(frozen=True, kw_only=True)
class PinChannelCliCommand(CliCommand, declared_name='pin-channel'):
    help = 'Set channel pin'
    name: str = option('--name', target_bound=True)
    pinned: bool = option('--pinned', default=False, target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, channel):
        return (cls(name=channel.name, pinned=not channel.pinned),) if channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_pinned(self.name, self.pinned)


@dataclass(frozen=True, kw_only=True)
class ChannelActivityCliCommand(CliCommand, declared_name='channel-activity'):
    help = 'Toggle member activity'
    name: str = option('--name', target_bound=True)
    enabled: bool = option('--enabled', default=False, target_bound=True)

    @classmethod
    def channel_bindings(cls, comms, channel):
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
    target: str = option('--target', target_bound=True)
    worktree: str = option('--worktree', default_factory=os.getcwd)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        return (cls(target=thread.name),)

    @classmethod
    def channel_bindings(cls, comms, channel):
        return (cls(target=channel.name),)

    def for_editor(self, comms, target, project):
        return replace(self, worktree=project)

    def apply(self, ctx: Comms) -> TargetReadResult:
        ctx.views.mark_user_view_read(self.target, worktree=self.worktree)
        return TargetReadResult(self.target)


@dataclass(frozen=True, kw_only=True)
class PinThreadCliCommand(CliCommand, declared_name='pin-thread'):
    help = 'Toggle thread pin in channel'
    name: str = option('--name', target_bound=True)
    channel: str = option('--channel', target_bound=True)
    pinned: bool = option('--pinned', default=False, target_bound=True)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        if channel is None:
            return ()
        document = comms.channels.catalog.read()
        if not document.resolve(channel).matches(thread.tags):
            return ()
        return (cls(name=thread.name, channel=channel,
                    pinned=thread.name not in document.pinned_threads(channel)),)

    def apply(self, ctx: Comms) -> ThreadPinnedResult:
        ctx.channels.set_thread_pinned(self.channel, self.name, self.pinned)
        return ThreadPinnedResult(self.name, self.pinned)
