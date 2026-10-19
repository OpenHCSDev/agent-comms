"""`agent-comms-commander TOOL ARGS...`: each module in this package that declares `main` is one tool."""
import argparse
import pkgutil
import sys
from importlib import import_module

import agent_comms.commander as package

from .config import CommanderConfig
from .order import Commander


def tool_name(module: str) -> str:
    """The tool a module of this package is: its name, with dashes."""
    return module.rsplit('.', 1)[1].replace('_', '-')


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--config', dest='config', type=CommanderConfig.read,
                           default=str(CommanderConfig.path()), metavar='JSON')
    selection.add_argument('--root', dest='config', type=CommanderConfig.read_root,
                           default=argparse.SUPPRESS, metavar='ROOT')
    options, argv = parser.parse_known_args(sys.argv[1:])
    commander = Commander.of(options.config)
    modules = (import_module(f'{package.__name__}.{m.name}') for m in pkgutil.iter_modules(package.__path__) if m.name != 'cli')
    tools = {tool_name(module.__name__): module for module in modules if hasattr(module, 'main')}
    if not argv or argv[0] not in tools:
        sys.exit(f'usage: agent-comms-commander {{{",".join(sorted(tools))}}} ARGS...')
    tools[argv[0]].main(argv[1:], commander)
