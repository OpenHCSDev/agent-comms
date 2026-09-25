"""OpenHCS agent communications — JSON CLI.

Every subcommand is one operation on the Comms wire, prints exactly one JSON
document on stdout, and exits 0 on success or 1 with ``{"error": ...}`` on
failure. This is the adapter surface for the pi extension and other
process-based clients: they shell out and parse JSON, they do not own
orchestration semantics.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .declarations import ActivityState, MessageType
from .exporting import WireExportFormat, WireExportLimit, WireExportScope
from .importing import ImportFormat, ImportLimits
from .operations import Comms, ForkSpec, wire


def _emit(payload: object) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _fail(message: str) -> int:
    json.dump({"error": message}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d|w)", value.strip().lower())
    if match is None:
        raise argparse.ArgumentTypeError("duration must look like 30s, 15m, 24h, 7d, or 2w")
    amount = float(match.group(1))
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-comms", description=__doc__)
    parser.add_argument(
        "--root",
        default=None,
        help="Comms root directory (default $AGENT_COMMS_ROOT or ~/.agent-comms)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tools", help="Emit the shared adapter tool catalog")

    p_repair = sub.add_parser(
        "repair-input-routing", help="Preview historical native-input attribution repair"
    )
    p_repair.add_argument("--apply", action="store_true", help="Persist verified input routing")

    p_import = sub.add_parser("import-thread", help="Import an OpenCode/Codex context snapshot")
    p_import.add_argument("--format", required=True, choices=[kind.value for kind in ImportFormat])
    p_import.add_argument(
        "--source", required=True, help="OpenCode export JSON/database or Codex rollout JSONL"
    )
    p_import.add_argument("--session-id", help="Required for OpenCode database imports")
    p_import.add_argument("--name", required=True, help="New agent-comms thread name")
    p_import.add_argument("--worktree", help="Override the source project directory")
    p_import.add_argument(
        "--model", help="Target Pi provider/model; not inferred from foreign model IDs"
    )
    p_import.add_argument("--tags", default="")
    p_import.add_argument("--max-messages", type=int, default=200)
    p_import.add_argument("--max-characters", type=int, default=120000)
    p_import.add_argument("--max-message-characters", type=int, default=12000)

    p_invoke = sub.add_parser("invoke", help="Invoke one declared adapter tool")
    p_invoke.add_argument("--tool", required=True)
    p_invoke.add_argument("--arguments", default="{}", help="Tool arguments as a JSON object")

    p_send = sub.add_parser("send", help="Send to a thread (DM), #channel, or #all")
    p_send.add_argument("--from", dest="sender", required=True)
    p_send.add_argument("--to", dest="target", required=True)
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--type", default="info", choices=[t.value for t in MessageType])

    p_inbox = sub.add_parser("inbox", help="Undelivered messages for a thread")
    p_inbox.add_argument("--thread", required=True)

    p_ack = sub.add_parser("ack", help="Mark inbox delivered")
    p_ack.add_argument("--thread", required=True)

    sub.add_parser("who", help="Presence: who is in the chat")

    sub.add_parser("status", help="Live activity: what each thread is doing right now")

    p_history = sub.add_parser("history", help="Full history of a DM, channel, or everything")
    p_history.add_argument("--with", dest="with_thread", default=None, help="DM with this thread")
    p_history.add_argument("--channel", default=None, help="Channel (#all, #tag)")
    p_history.add_argument("--everything", action="store_true", help="Every message on the wire")
    p_history.add_argument("--as", dest="me", default=None, help="Your thread (for --with)")

    p_export = sub.add_parser("export-wire", help="Export durable IRC/wire message history")
    p_export.add_argument("--output", required=True, help="Destination file")
    p_export.add_argument(
        "--format",
        default=WireExportFormat.JSONL.value,
        choices=[kind.value for kind in WireExportFormat],
    )
    export_scope = p_export.add_mutually_exclusive_group(required=True)
    export_scope.add_argument("--everything", action="store_true", help="Every wire message")
    export_scope.add_argument("--channel", help="One target-owned #channel")
    export_scope.add_argument("--dm", nargs=2, metavar=("FIRST", "SECOND"), help="One canonical DM")
    export_limit = p_export.add_mutually_exclusive_group(required=True)
    export_limit.add_argument("--full", action="store_true", help="Export without a size/time cap")
    export_limit.add_argument("--max-bytes", type=int, help="Hard UTF-8 artifact byte ceiling")
    export_limit.add_argument(
        "--last", type=_duration_seconds, metavar="DURATION", help="Recent window, e.g. 24h"
    )
    p_export.add_argument("--overwrite", action="store_true", help="Replace an existing output")

    sub.add_parser("channels", help="Derived channel list")

    p_threads = sub.add_parser("threads", help="List threads")
    p_threads.add_argument("--active-only", action="store_true")

    p_detail = sub.add_parser("thread", help="Detail for one thread")
    p_detail.add_argument("--name", required=True)
    p_detail.add_argument(
        "--no-pending", action="store_true", help="Skip the inbox count for fast metadata reads"
    )

    p_register = sub.add_parser("register", help="Register a thread")
    p_register.add_argument("--name", required=True)
    p_register.add_argument("--worktree", required=True)
    p_register.add_argument("--tags", default="")
    p_register.add_argument("--parent", default=None)
    p_register.add_argument("--task", default=None)
    p_register.add_argument("--pid", type=int, default=0)

    p_heartbeat = sub.add_parser("heartbeat", help="Mark thread running")
    p_heartbeat.add_argument("--name", required=True)

    p_attach = sub.add_parser(
        "attach-session", help="Attach a live Pi session to a registered thread"
    )
    p_attach.add_argument("--name", required=True)
    p_attach.add_argument("--session-file", required=True)
    p_attach.add_argument("--pid", type=int, default=None)

    p_activity = sub.add_parser("activity", help="Publish current thread activity")
    p_activity.add_argument("--name", required=True)
    p_activity.add_argument(
        "--state", choices=[state.value for state in ActivityState], required=True
    )
    p_activity.add_argument("--detail", default="")

    p_stop = sub.add_parser("stop", help="Mark thread stopped")
    p_stop.add_argument("--name", required=True)

    p_restart = sub.add_parser("restart", help="Restart idle running owners to reload backend code")
    restart_scope = p_restart.add_mutually_exclusive_group(required=True)
    restart_scope.add_argument("--name", help="One running thread (aliases supported)")
    restart_scope.add_argument(
        "--all", action="store_true", help="All running owners; excludes stopped threads"
    )
    p_restart.add_argument("--agent-bin", default=os.environ.get("AGENT_COMMS_AGENT_BIN", "pi"))
    p_restart.add_argument("--agent-args", default=os.environ.get("AGENT_COMMS_AGENT_ARGS"))

    p_release = sub.add_parser("release", help="Voluntarily mark the calling thread stopped")
    p_release.add_argument("--name", required=True)

    p_archive = sub.add_parser("archive", help="Archive a stopped thread")
    p_archive.add_argument("--name", required=True)

    p_delete = sub.add_parser("delete", help="Permanently delete a stopped thread")
    p_delete.add_argument("--name", required=True)

    p_rename = sub.add_parser("rename-self", help="Rename your own running thread")
    p_rename.add_argument("--to", dest="new_name", required=True)

    p_fork = sub.add_parser("fork", help="Fork a child pi thread")
    p_fork.add_argument("--name", required=True)
    p_fork.add_argument("--parent", required=True)
    p_fork.add_argument("--task", required=True)
    p_fork.add_argument("--tags", default="")
    p_fork.add_argument("--prompt", default=None)
    p_fork.add_argument("--pi-bin", default=os.environ.get("AGENT_COMMS_AGENT_BIN", "pi"))

    p_ledger = sub.add_parser("ledger", help="Read or merge ledger state")
    p_ledger.add_argument("--merge-from", dest="merge_from", default=None)
    p_ledger.add_argument("--author", default=None)

    p_poll = sub.add_parser("poll", help="Status snapshot: thread, inbox, ledger, peers")
    p_poll.add_argument("--thread", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    comms: Comms = wire(Path(args.root).expanduser() if args.root else None)

    try:
        if args.command == "tools":
            from .tools import tool_catalog

            _emit({"tools": tool_catalog()})
        elif args.command == "import-thread":
            receipt = comms.import_thread(
                args.source,
                ImportFormat(args.format),
                name=args.name,
                session_id=args.session_id,
                worktree=args.worktree,
                model=args.model,
                tags=frozenset(tag.strip() for tag in args.tags.split(",") if tag.strip()),
                limits=ImportLimits(
                    args.max_messages, args.max_characters, args.max_message_characters
                ),
            )
            _emit(receipt.to_wire())
        elif args.command == "invoke":
            from .tools import invoke_tool

            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                return _fail("invoke --arguments must be a JSON object")
            _emit(invoke_tool(comms, args.tool, arguments))
        elif args.command == "send":
            mid = comms.send(args.sender, args.target, args.body, MessageType(args.type))
            _emit({"id": mid})
        elif args.command == "inbox":
            _emit({"messages": [m.to_wire() for m in comms.inbox(args.thread)]})
        elif args.command == "ack":
            _emit({"acknowledged": comms.acknowledge(args.thread)})
        elif args.command == "who":
            _emit({"who": list(comms.who())})
        elif args.command == "status":
            _emit(
                {
                    "status": [
                        {
                            "thread": thread,
                            "state": activity.state.value,
                            "detail": activity.detail,
                            "ts": activity.timestamp,
                        }
                        for thread, activity in sorted(comms.all_activity().items())
                    ]
                }
            )
        elif args.command == "channels":
            _emit({"channels": list(comms.channels())})
        elif args.command == "export-wire":
            started_at = time.time()
            if args.everything:
                scope = WireExportScope.everything()
            elif args.channel:
                scope = WireExportScope.for_channel(args.channel)
            else:
                scope = WireExportScope.for_dm(*args.dm)
            if args.full:
                export_limit = WireExportLimit.full()
            elif args.max_bytes is not None:
                export_limit = WireExportLimit.max_bytes(args.max_bytes)
            else:
                export_limit = WireExportLimit.recent(started_at - args.last)
            export_receipt = comms.export_wire(
                args.output,
                format=WireExportFormat(args.format),
                scope=scope,
                limit=export_limit,
                overwrite=args.overwrite,
                export_started_at=started_at,
            )
            _emit(export_receipt.to_wire())
        elif args.command == "history":
            selected = sum(bool(x) for x in (args.with_thread, args.channel, args.everything))
            if selected != 1:
                return _fail("history takes exactly one of --with, --channel, --everything")
            if args.with_thread:
                if not args.me:
                    return _fail("history --with requires --as (your thread)")
                _emit({"dm": [m.to_wire() for m in comms.dm_history(args.me, args.with_thread)]})
            elif args.channel:
                _emit(
                    {
                        "channel": args.channel,
                        "messages": [m.to_wire() for m in comms.channel_history(args.channel)],
                    }
                )
            else:
                _emit({"everything": [m.to_wire() for m in comms.full_history()]})
        elif args.command == "repair-input-routing":
            _emit(comms.repair_input_routing(dry_run=not args.apply))
        elif args.command == "threads":
            _emit({"threads": list(comms.list_threads(active_only=args.active_only))})
        elif args.command == "thread":
            _emit(comms.thread_detail(args.name, include_pending=not args.no_pending))
        elif args.command == "register":
            from .declarations import Thread

            thread = Thread(
                name=args.name,
                tags=frozenset(t.strip() for t in args.tags.split(",") if t.strip()),
                worktree=args.worktree,
                parent=args.parent,
                task=args.task,
                pid=args.pid,
            )
            comms.register(thread)
            _emit({"registered": args.name})
        elif args.command == "heartbeat":
            comms.heartbeat(args.name)
            _emit({"heartbeat": args.name})
        elif args.command == "attach-session":
            attached = comms.attach_session(args.name, args.session_file, pid=args.pid)
            _emit(
                {
                    "attached": attached.name,
                    "session_file": attached.session_file,
                    "pid": attached.pid,
                }
            )
        elif args.command == "activity":
            comms.set_activity(args.name, ActivityState(args.state), args.detail)
            _emit({"activity": args.name, "state": args.state})
        elif args.command == "stop":
            comms.stop(args.name)
            _emit({"stopped": args.name})
        elif args.command == "restart":
            results = comms.restart_owners(
                None if args.all else [args.name],
                agent_bin=args.agent_bin,
                agent_args=shlex.split(args.agent_args) if args.agent_args is not None else None,
            )
            _emit({"restarted": [asdict(result) for result in results]})
        elif args.command == "release":
            comms.release(args.name)
            _emit({"released": args.name})
        elif args.command == "archive":
            comms.archive(args.name)
            _emit({"archived": args.name})
        elif args.command == "delete":
            delete_result = comms.delete(args.name)
            _emit(
                {
                    "deleted": delete_result.name,
                    "messages_removed": delete_result.messages_removed,
                    "markers_removed": delete_result.markers_removed,
                    "activity_events_removed": delete_result.activity_events_removed,
                    "runtime_removed": delete_result.runtime_removed,
                    "ledger_references_removed": delete_result.ledger_references_removed,
                    "detached_children": list(delete_result.detached_children),
                }
            )
        elif args.command == "rename-self":
            rename_result = comms.rename_self(args.new_name)
            _emit(
                {
                    "previous": rename_result.previous,
                    "current": rename_result.current,
                    "changed": rename_result.changed,
                }
            )
        elif args.command == "fork":
            spec = ForkSpec(
                name=args.name,
                parent=args.parent,
                task=args.task,
                tags=frozenset(t.strip() for t in args.tags.split(",") if t.strip()),
                prompt=args.prompt,
            )
            child = comms.fork(spec, pi_bin=args.pi_bin)
            _emit({"forked": child.name, "pid": child.pid})
        elif args.command == "ledger":
            if args.merge_from is not None:
                if not args.author:
                    return _fail("ledger merge requires --author")
                updates = json.loads(Path(args.merge_from).read_text())
                comms.ledger_merge(updates, args.author)
                _emit({"merged": True})
            else:
                _emit(comms.ledger_read())
        elif args.command == "poll":
            _emit(comms.poll(args.thread))
        else:  # pragma: no cover - argparse enforces choices
            return _fail(f"unknown command {args.command!r}")
    except Exception as exc:
        return _fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
