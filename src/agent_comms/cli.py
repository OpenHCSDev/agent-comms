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
import sys
from collections.abc import Sequence
from pathlib import Path

from .declarations import MessageType
from .operations import Comms, ForkSpec, wire


def _emit(payload: object) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _fail(message: str) -> int:
    json.dump({"error": message}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-comms", description=__doc__)
    parser.add_argument(
        "--root",
        default=None,
        help="Comms root directory (default $AGENT_COMMS_ROOT or ~/.agent-comms)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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

    sub.add_parser("channels", help="Derived channel list")

    p_threads = sub.add_parser("threads", help="List threads")
    p_threads.add_argument("--active-only", action="store_true")

    p_detail = sub.add_parser("thread", help="Detail for one thread")
    p_detail.add_argument("--name", required=True)

    p_register = sub.add_parser("register", help="Register a thread")
    p_register.add_argument("--name", required=True)
    p_register.add_argument("--worktree", required=True)
    p_register.add_argument("--tags", default="")
    p_register.add_argument("--parent", default=None)
    p_register.add_argument("--task", default=None)
    p_register.add_argument("--pid", type=int, default=0)

    p_heartbeat = sub.add_parser("heartbeat", help="Mark thread running")
    p_heartbeat.add_argument("--name", required=True)

    p_stop = sub.add_parser("stop", help="Mark thread stopped")
    p_stop.add_argument("--name", required=True)

    p_fork = sub.add_parser("fork", help="Fork a child pi thread")
    p_fork.add_argument("--name", required=True)
    p_fork.add_argument("--parent", required=True)
    p_fork.add_argument("--task", required=True)
    p_fork.add_argument("--tags", default="")
    p_fork.add_argument("--prompt", default=None)
    p_fork.add_argument("--pi-bin", default="pi")

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
        if args.command == "send":
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
        elif args.command == "threads":
            _emit({"threads": list(comms.list_threads(active_only=args.active_only))})
        elif args.command == "thread":
            _emit(comms.thread_detail(args.name))
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
        elif args.command == "stop":
            comms.stop(args.name)
            _emit({"stopped": args.name})
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
