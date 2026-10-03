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
from dataclasses import MISSING, asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Self, get_args, get_origin, get_type_hints

from .activity import ActivityState
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
    **parser_options: Any,
) -> Any:
    """One field owns both its CLI projection and JSON boundary conversion."""
    metadata = {
        "multiline": multiline,
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


@dataclass(frozen=True, kw_only=True)
class CliCommand(DeclaredFamily, Command, affix="CliCommand"):
    help: ClassVar[str]

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None) -> tuple[dict[str, object], ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms, channel) -> tuple[dict[str, object], ...]:
        return ()

    @classmethod
    def bindings(cls, comms: Comms, target: str, channel: str | None = None) -> tuple[dict[str, object], ...]:
        from .channel_targets import is_channel_target
        if is_channel_target(target):
            return cls.channel_bindings(comms, comms.channels.catalog.read().resolve(target))
        snapshot = comms.registry.snapshot()
        thread = snapshot.require(target)
        return cls.thread_bindings(comms, thread, snapshot.status(thread.name), channel)

    @classmethod
    def target_catalog(cls, comms: Comms, target: str, channel: str | None = None, *, project: str) -> list[dict[str, object]]:
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
        return [member.describe(bound, comms, target, project)
                for member, available in bindings for bound in available]

    @classmethod
    def describe(cls, bound: dict[str, object], comms: Comms, target: str, project: str) -> dict[str, object]:
        schema = FieldCodec.record_schema(cls)
        schema['properties'] = {key: value for key, value in schema['properties'].items()
                                if key not in bound and key != cls.family_discriminator}
        schema['required'] = [key for key in schema['required'] if key in schema['properties']]
        defaults = cls.editor_defaults(comms, target, project)
        for declared in fields(cls):
            key = declared.metadata.get('wire_name', declared.name)
            if key in schema['properties']:
                schema['properties'][key]['multiline'] = declared.metadata['multiline']
                schema['properties'][key]['editor_default'] = defaults.get(key, '')
                schema['properties'][key]['description'] = declared.metadata['parser_options'].get(
                    'help', key.replace('_', ' ').capitalize())
        return {'command': cls.declared_name, 'label': cls.help, 'parameters': schema,
                'confirmation': cls.confirmation(bound)}

    @classmethod
    def reconnect_targets(cls, result) -> tuple[str, ...]:
        return ()

    @classmethod
    def editor_defaults(cls, comms, target, project) -> dict[str, str]:
        result = {}
        for declared in fields(cls):
            default = (declared.default if declared.default is not MISSING else
                       declared.default_factory() if declared.default_factory is not MISSING else None)
            result[declared.metadata.get('wire_name', declared.name)] = (
                '' if default is None else default if isinstance(default, str) else json.dumps(FieldCodec.encode(default)))
        return result

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
            elif annotation is str or str in get_args(annotation):
                result[key] = text
            else:
                result[key] = json.loads(text)
        return result

    @classmethod
    def confirmation(cls, bound: dict[str, object]) -> str:
        return ''

    @classmethod
    def execute_target(cls, comms: Comms, target: str, arguments: dict[str, object],
                       *, confirmed: bool = False, channel: str | None = None) -> object:
        bindings = cls.bindings(comms, target, channel)
        if len(bindings) != 1:
            raise ValueError('This action is no longer available for the target')
        bound, = bindings
        if bound.keys() & arguments.keys():
            raise ValueError('Target-bound parameters cannot be overridden')
        if cls.confirmation(bound) and not confirmed:
            raise ValueError(cls.confirmation(bound))
        return cls.from_payload({'kind': cls.declared_name, **bound, **arguments}).apply(comms)

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
    name: str = option("--name")

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        from .tools import CommsStopTool
        return ({'name': thread.name},) if CommsStopTool.available_for_thread(thread, status) else ()

    def apply(self, ctx: Comms) -> Any:
        ctx.owners.stop(self.name)
        return {"stopped": self.name}


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
    name: str = option("--name")

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        from .tools import CommsArchiveTool
        return ({'name': thread.name},) if CommsArchiveTool.available_for_thread(thread, status) else ()

    def apply(self, ctx: Comms) -> Any:
        ctx.threads.archive(self.name)
        return {"archived": self.name}




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
    parent: str = option("--parent")
    task: str = option("--task", default="", multiline=True)
    tags: frozenset[str] | None = option(
        "--tags", default=None, normalize=lambda value: _tags(value) if value is not None else None
    )
    prompt: str | None = option("--prompt", default=None, multiline=True)
    pi_bin: str = option(
        "--pi-bin", default_factory=lambda: os.environ.get("AGENT_COMMS_AGENT_BIN", "pi")
    )

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        from .tools import CommsForkTool
        return ({'parent': thread.name},) if CommsForkTool.available_for_thread(thread, status) else ()

    @classmethod
    def editor_defaults(cls, comms, target, project):
        result = super().editor_defaults(comms, target, project)
        result['tags'] = ', '.join(sorted(comms.registry.require(target).tags))
        return result

    def apply(self, ctx: Comms) -> Any:
        spec = ForkSpec(
            name=self.name, parent=self.parent, task=self.task, tags=self.tags, prompt=self.prompt
        )
        child = ctx.threads.fork(spec, pi_bin=self.pi_bin)
        return {"forked": child.name, "pid": child.pid}


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
        from .turn_context import TurnContext, NextContextTurn

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
            return {"manifests": FieldCodec.encode(selected), "text_recorded": False}
        owner = ctx.registry.require(self.thread)
        context = TurnContext.for_owner(owner, NextContextTurn(), "", ctx.views.thread_views())
        for segment in owner.context_goal_segments():
            context = context.prepend(segment)
        for segment in ctx.bus.awareness_segments(owner):
            context = context.append(segment)
        launch = PrivateNkLaunch.from_environment(
            ctx.root, ctx.owners.restart_environment(os.environ)
        )
        if launch is None:
            raise ValueError("Context inspection requires this root's configured native package")
        counter = NativeTokenCounter(launch.native_package)
        counts = counter.measure(tuple(segment.text() for segment in context.segments))
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

    def apply(self, ctx: Comms) -> Any:
        return {'actions': CliCommand.target_catalog(ctx, self.target, self.channel, project=self.project)}


@dataclass(frozen=True, kw_only=True)
class TargetActionCliCommand(CliCommand, declared_name='target-action'):
    channel: str | None = option('--channel', default=None)
    help = 'Execute a declared operation against its current target'
    target: str = option('--target')
    operation: str = option('--operation')
    arguments: dict[str, Any] = option('--arguments', default_factory=dict,
                                     parser_default='{}', normalize=_json_object)
    confirmed: bool = option('--confirmed', default=False)

    def apply(self, ctx: Comms) -> Any:
        return CliCommand.decode(self.operation).execute_target(
            ctx, self.target, self.arguments, confirmed=self.confirmed, channel=self.channel)


@dataclass(frozen=True, kw_only=True)
class StartCliCommand(CliCommand):
    help = 'Start thread'
    name: str = option('--name')

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        from .tools import CommsStartTool
        return ({'name': thread.name},) if CommsStartTool.available_for_thread(thread, status) else ()

    @classmethod
    def reconnect_targets(cls, result):
        return (result['thread'],) if result['launched'] else ()

    def apply(self, ctx: Comms) -> Any:
        return asdict(ctx.owners.start(self.name))


@dataclass(frozen=True, kw_only=True)
class ThreadTagsCliCommand(CliCommand, declared_name='thread-tags'):
    help = 'Edit thread tags'
    name: str = option('--name')
    tags: frozenset[str] = option('--tags', normalize=_tags,
                                help='Complete tags, separated by commas')

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        return ({'name': thread.name},)

    @classmethod
    def editor_defaults(cls, comms, target, project):
        return {'tags': ', '.join(sorted(comms.registry.require(target).tags))}

    def apply(self, ctx: Comms) -> Any:
        thread = ctx.channels.replace_tags(self.name, self.tags)
        return {'thread': thread.name, 'tags': sorted(thread.tags)}


@dataclass(frozen=True, kw_only=True)
class ExactTagCliCommand(CliCommand):
    name: str = option('--name')

    @classmethod
    def channel_bindings(cls, comms, channel):
        return ({'name': channel.name.removeprefix('#')},) if channel.exact else ()


@dataclass(frozen=True, kw_only=True)
class RenameTagCliCommand(ExactTagCliCommand, declared_name='rename-tag'):
    help = 'Rename channel tag'
    new_name: str = option('--to', help='New tag name')

    def apply(self, ctx: Comms) -> Any:
        ctx.channels.rename_tag(self.name, self.new_name)
        return {'renamed': self.name, 'name': self.new_name}


@dataclass(frozen=True, kw_only=True)
class DeleteTagCliCommand(ExactTagCliCommand, declared_name='delete-tag'):
    help = 'Delete channel tag'

    @classmethod
    def confirmation(cls, bound):
        return f"Delete tag #{bound['name']} from all threads? Saved views referencing it must be removed first."

    def apply(self, ctx: Comms) -> Any:
        ctx.channels.delete_tag(self.name)
        return {'deleted_tag': self.name}


@dataclass(frozen=True, kw_only=True)
class DeleteViewCliCommand(CliCommand, declared_name='delete-view'):
    help = 'Delete saved view'
    name: str = option('--name')

    @classmethod
    def channel_bindings(cls, comms, channel):
        return ({'name': channel.view.name},) if channel.view is not None else ()

    @classmethod
    def confirmation(cls, bound):
        return f"Delete saved view #{bound['name']}? Thread tags and messages are preserved."

    def apply(self, ctx: Comms) -> Any:
        ctx.channels.delete_saved_view(self.name)
        return {'deleted_view': self.name}


@dataclass(frozen=True, kw_only=True)
class PinChannelCliCommand(CliCommand, declared_name='pin-channel'):
    help = 'Set channel pin'
    name: str = option('--name')
    pinned: bool = option('--pinned', default=False)

    @classmethod
    def channel_bindings(cls, comms, channel):
        return ({'name': channel.name, 'pinned': not channel.pinned},) if channel.exact else ()

    def apply(self, ctx: Comms) -> Any:
        return ctx.channels.set_channel_pinned(self.name, self.pinned).to_wire()


@dataclass(frozen=True, kw_only=True)
class ChannelActivityCliCommand(CliCommand, declared_name='channel-activity'):
    help = 'Toggle member activity'
    name: str = option('--name')
    enabled: bool = option('--enabled', default=False)

    @classmethod
    def channel_bindings(cls, comms, channel):
        return ({'name': channel.name, 'enabled': not channel.any_mode},) if channel.exact else ()

    def apply(self, ctx: Comms) -> Any:
        return ctx.channels.set_channel_any_mode(self.name, self.enabled).to_wire()


@dataclass(frozen=True, kw_only=True)
class TargetEditCliCommand(TargetActionCliCommand, declared_name='target-edit'):
    help = 'Execute a declared operation using its original CLI field parsers'

    def apply(self, ctx: Comms) -> Any:
        command = CliCommand.decode(self.operation)
        arguments = command.editor_arguments(self.arguments)
        return command.execute_target(ctx, self.target, arguments,
                                      confirmed=self.confirmed, channel=self.channel)


@dataclass(frozen=True, kw_only=True)
class ReadTargetCliCommand(CliCommand, declared_name='read-target'):
    help = 'Mark view read'
    target: str = option('--target')
    worktree: str = option('--worktree', default_factory=os.getcwd)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        return ({'target': thread.name},)

    @classmethod
    def channel_bindings(cls, comms, channel):
        return ({'target': channel.name},)

    @classmethod
    def editor_defaults(cls, comms, target, project):
        return {'worktree': project}

    def apply(self, ctx: Comms) -> Any:
        ctx.views.mark_user_view_read(self.target, worktree=self.worktree)
        return {'read': self.target}


@dataclass(frozen=True, kw_only=True)
class PinThreadCliCommand(CliCommand, declared_name='pin-thread'):
    help = 'Toggle thread pin in channel'
    name: str = option('--name')
    channel: str = option('--channel')
    pinned: bool = option('--pinned', default=False)

    @classmethod
    def thread_bindings(cls, comms, thread, status, channel=None):
        if channel is None:
            return ()
        document = comms.channels.catalog.read()
        if not document.resolve(channel).matches(thread.tags):
            return ()
        return ({'name': thread.name, 'channel': channel,
                 'pinned': thread.name not in document.pinned_threads(channel)},)

    def apply(self, ctx: Comms) -> Any:
        ctx.channels.set_thread_pinned(self.channel, self.name, self.pinned)
        return {'thread': self.name, 'pinned': self.pinned}
