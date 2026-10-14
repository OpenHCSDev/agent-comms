#!/home/ts/.local/share/agent-comms/runtimes/toad-86d9e5c49-core-ccec96b85-native-dba4356e87520876-20261014T165633Z/bin/python
"""Claude Code Monitor entrypoint for the shared line-notification transport."""
from pathlib import Path

from cli_comms import LineMonitorBridge, Options, parse_options, run_bridge, shared_parser


def main() -> None:
    parser = shared_parser(__doc__, Path.home() / '.local/state/claude-comms-inbox',
                           LineMonitorBridge)
    run_bridge(LineMonitorBridge, parse_options(parser, Options))


if __name__ == '__main__':
    main()
