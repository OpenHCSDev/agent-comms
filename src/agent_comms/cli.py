"""OpenHCS agent communications — JSON CLI.

Commands print one JSON result, except watch, which streams messages as JSON
lines. They exit 0 on success or 1 with ``{"error": ...}`` on failure. This is the adapter surface for the pi extension and other
process-based clients: they shell out and parse JSON, they do not own
orchestration semantics.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from .cli_commands import CliCommand
from .comms import wire
from .owner_restart import StoppedOwnerFailure


def _fail(error: BaseException) -> int:
    """Report the failure with every cause, so a translated error never hides its origin."""
    message = str(error)
    cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    while cause is not None:
        message += f"; caused by {type(cause).__name__}: {cause}"
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
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
        return command.run(comms, sys.stdout)
    except KeyboardInterrupt:
        return 130
    except StoppedOwnerFailure as exc:
        # Do not ignore a failure after starting to stop an owner.
        exc.abandon()
        return _fail(exc)
    except Exception as exc:
        return _fail(exc)
    finally:
        route_guard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
