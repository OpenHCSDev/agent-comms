"""Lightweight CLI dispatcher for `agent-comms-inbox-watch`.

Routes `--wait-log` directly to `agent_comms.cli_bridges.wait_log` without importing `aiohttp`
or the full `inbox_watch_client` WebSocket stack, and delegates live watch URLs to
`agent_comms.cli_bridges.inbox_watch_client`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_comms.cli_bridges.wait_log import wait_for_log_growth_sync


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch a connector inbox URL or wait event-driven for a persistent watch log."
    )
    parser.add_argument("url", nargs="?", help="The one-use URL comms_watch returned")
    parser.add_argument(
        "--transcript",
        type=Path,
        default=None,
        help="Explicit Antigravity transcript.jsonl path to stream alongside the inbox watch",
    )
    parser.add_argument(
        "--wait-log",
        type=Path,
        default=None,
        help="Wait event-driven for a persistent watch log to grow, print new lines, and exit 0",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Starting byte offset for --wait-log (default: saved cursor, or 0 for a new log; never skips past a saved cursor)",
    )
    args = parser.parse_args()
    if args.wait_log is not None:

        chunk = wait_for_log_growth_sync(args.wait_log, offset=args.offset)
        sys.stdout.write(chunk)
        sys.stdout.flush()
        sys.exit(0)
    if not args.url:
        parser.error("the following arguments are required: url (or pass --wait-log PATH)")
    from agent_comms.cli_bridges.inbox_watch_client import run_watch_url

    run_watch_url(args.url, args.transcript)


if __name__ == "__main__":
    main()
