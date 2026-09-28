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
from .command import Command
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
    WireExportFormat,
)
from .importing import ImportFormat, ImportLimits
from .messages import MessageType
from .thread_management import ForkSpec


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


def option(
    *flags: str,
    default: Any = MISSING,
    default_factory: Any = MISSING,
    group: str | None = None,
    normalize: Callable[..., Any] | None = None,
    wire_name: str | None = None,
    parser_default: Any = MISSING,
    parser_default_factory: Any = MISSING,
    **parser_options: Any,
) -> Any:
    """One field owns both its CLI projection and JSON boundary conversion."""
    metadata = {
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
            owner.add_argument(
                *metadata["flags"], dest=metadata.get("wire_name", declared.name), **options
            )

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
    format: WireExportFormat = option("--format", default=JsonlFormat())
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

    def apply(self, ctx: Comms) -> Any:
        from .threads import Thread

        thread = Thread(
            name=self.name,
            tags=self.tags,
            worktree=self.worktree,
            parent=self.parent,
            task=self.task,
            pid=self.pid,
        )
        ctx.threads.register(thread)
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
        attached = ctx.threads.attach_session(self.name, self.session_file, pid=self.pid)
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
    agent_bin: str = option(
        "--agent-bin", default_factory=lambda: os.environ.get("AGENT_COMMS_AGENT_BIN", "pi")
    )
    agent_args: list[str] | None = option(
        "--agent-args",
        default=None,
        parser_default_factory=lambda: os.environ.get("AGENT_COMMS_AGENT_ARGS"),
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

    def apply(self, ctx: Comms) -> Any:
        ctx.threads.archive(self.name)
        return {"archived": self.name}


@dataclass(frozen=True, kw_only=True)
class DeleteCliCommand(CliCommand):
    help = "Permanently delete a stopped thread"
    name: str = option("--name")

    def apply(self, ctx: Comms) -> Any:
        delete_result = ctx.threads.delete(self.name)
        return {
            "deleted": delete_result.name,
            "messages_removed": delete_result.messages_removed,
            "markers_removed": delete_result.markers_removed,
            "activity_events_removed": delete_result.activity_events_removed,
            "runtime_removed": delete_result.runtime_removed,
            "ledger_references_removed": delete_result.ledger_references_removed,
            "detached_children": list(delete_result.detached_children),
        }


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
    task: str = option("--task")
    tags: frozenset[str] = option(
        "--tags", default_factory=frozenset, parser_default="", normalize=_tags
    )
    prompt: str | None = option("--prompt", default=None)
    pi_bin: str = option(
        "--pi-bin", default_factory=lambda: os.environ.get("AGENT_COMMS_AGENT_BIN", "pi")
    )

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
        from .compaction_journal import CompactionJournal
        from .field_codec import FieldCodec

        thread = ctx.registry.require(self.thread)
        path = ctx.root / "compaction-commits.sqlite3"
        if not thread.session_file or not path.exists():
            return {"thread": thread.name, "attempts": []}
        journal = CompactionJournal(path)
        return {
            "thread": thread.name,
            "attempts": [
                {"operation_id": row.operation_id, "state": FieldCodec.encode(row.state)}
                for row in journal.selected_summaries(thread.session_file)
            ],
        }
