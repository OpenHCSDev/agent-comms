"""OpenHCS agent communications — JSON CLI.

Commands print one JSON result, except watch, which streams messages as JSON
lines. Errors are one JSON line carrying a declared type and error text.
They exit 0 on success or 1 on failure. This is the adapter surface for the pi extension and other
process-based clients: they shell out and parse JSON, they do not own
orchestration semantics.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from .active_route import resolve_comms_route
from .native_settings import NativeThreadDefaults
from .cli_commands import CliCommand
from .tool_caller import ToolCaller
from .cli_protocol import CoreError
from .comms import Comms
from .owner_restart import StoppedOwnerFailure
from .launch_runtime import LaunchRuntime


def _fail(error: BaseException) -> int:
    """The declared wire family owns classification, causes and JSON encoding."""
    print(CoreError.from_exception(error).model_dump_json(), flush=True)
    return 1


class CliArgumentParser(argparse.ArgumentParser):
    """Argument errors use Core's JSON failure boundary, including subcommands."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser(settings: NativeThreadDefaults) -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="agent-comms", description=__doc__)
    parser.add_argument(
        "--root",
        default=None,
        help="Comms root directory (default $AGENT_COMMS_ROOT, active route, or ~/.agent-comms)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for declaration in CliCommand.members_with(CliCommand):
        declaration.add_parser(subparsers, settings)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        with ExitStack() as route_guard:
            runtime = LaunchRuntime.current()
            settings = NativeThreadDefaults()
            parser = build_parser(settings)
            args = argparse.Namespace()
            remaining = list(sys.argv[1:] if argv is None else argv)
            caller = ToolCaller.from_arguments(parser, remaining, args)
            if remaining:
                parser.error(f"unrecognized arguments: {' '.join(remaining)}")
            command = CliCommand.from_namespace(args)
            route = resolve_comms_route(Path(args.root).expanduser() if args.root else None)
            route_guard.enter_context(route.admit_client())
            comms = Comms(route, runtime=runtime)
            return command.run(comms, sys.stdout, caller=caller)
    except KeyboardInterrupt:
        return 130
    except StoppedOwnerFailure as exc:
        # Do not ignore a failure after starting to stop an owner.
        exc.abandon()
        return _fail(exc)
    except Exception as exc:
        return _fail(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
