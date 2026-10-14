"""Claude Code Monitor entrypoint for the shared line-notification transport."""
from pathlib import Path

from .shared import LineMonitorBridge, Options, parse_options, run_bridge, shared_parser


def main() -> None:
    parser = shared_parser(__doc__, Path.home() / '.local/state/claude-comms-inbox',
                           LineMonitorBridge)
    run_bridge(LineMonitorBridge, parse_options(parser, Options))

