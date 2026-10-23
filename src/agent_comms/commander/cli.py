"""`agent-comms-commander TOOL ARGS...`: each module in this package that declares `main` is one tool."""
import argparse
import pkgutil
import sys
from importlib import import_module
from pathlib import Path

import agent_comms.commander as package
from agent_comms.active_route import resolve_comms_route
from agent_comms.errors import ToolRefusedError

from .order import Commander


def tool_name(module: str) -> str:
    """The tool a module of this package is: its name, with dashes."""
    return module.rsplit('.', 1)[1].replace('_', '-')


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--root', type=Path, metavar='ROOT')
    options, argv = parser.parse_known_args(sys.argv[1:])
    modules = (import_module(f'{package.__name__}.{m.name}') for m in pkgutil.iter_modules(package.__path__) if m.name != 'cli')
    tools = {tool_name(module.__name__): module for module in modules if hasattr(module, 'main')}
    if not argv or argv[0] not in tools:
        sys.exit(f'usage: agent-comms-commander {{{",".join(sorted(tools))}}} ARGS...')
    try:
        context = Commander.read_root(resolve_comms_route(options.root).root)
        tools[argv[0]].main(argv[1:], context)
    except ToolRefusedError as refused:
        sys.exit(f'Refused: {refused}')
