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
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from .cli_commands import CliCommand
from .comms import wire
from .owner_restart import StoppedOwnerFailure


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
        help="Comms root directory (default $AGENT_COMMS_ROOT, active route, or ~/.agent-comms)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for declaration in CliCommand.members_with(CliCommand):
        declaration.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    route_guard = ExitStack()
    try:
        command = CliCommand.from_namespace(args)
        from .active_route import resolve_comms_route

        route = resolve_comms_route(Path(args.root).expanduser() if args.root else None)
        route_guard.enter_context(route.admit_client())
        comms = wire(route)
        _emit(command.encode_result(command.apply(comms)))
    except StoppedOwnerFailure as exc:
        # This JSON adapter is one-shot. It cannot silently discard acquired
        # custody as though the error had preceded retirement.
        exc.abandon()
        return _fail(f'{exc}; original failure: {exc.__cause__}')
    except Exception as exc:
        return _fail(str(exc))
    finally:
        route_guard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
