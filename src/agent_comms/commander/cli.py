"""`agent-comms-commander TOOL ARGS...`: each module in this package that declares `main` is one tool."""
import argparse
import pkgutil
import sys
from importlib import import_module
from pathlib import Path

import agent_comms.commander as package


def tool_name(module: str) -> str:
    """The tool a module of this package is: its name, with dashes."""
    return module.rsplit('.', 1)[1].replace('_', '-')


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--root', type=Path, metavar='ROOT')
    options, argv = parser.parse_known_args(sys.argv[1:])
    modules = {
        tool_name(f'{package.__name__}.{info.name}'): f'{package.__name__}.{info.name}'
        for info in pkgutil.iter_modules(package.__path__) if info.name != 'cli'
    }
    if not argv or argv[0] not in modules:
        # Help needs every tool's documentation; executing one needs only that module.
        loaded = {name: import_module(module) for name, module in modules.items()}
        tools = {name: module for name, module in loaded.items() if hasattr(module, 'main')}
        lines = [f'usage: agent-comms-commander {{{",".join(sorted(tools))}}} ARGS...', '']
        lines += [f'  {name:<11} {tools[name].__doc__.splitlines()[0]}'
                  for name in sorted(tools)]
        asked = argv[:1] in (['-h'], ['--help'])
        print('\n'.join(lines), file=sys.stdout if asked else sys.stderr)
        sys.exit(0 if asked else 2)
    selected = import_module(modules[argv[0]])
    try:
        entry = selected.main
    except AttributeError:
        parser.error(f'{argv[0]} does not declare a commander tool')
    from agent_comms.active_route import ActiveRoute, resolve_comms_route
    from agent_comms.cli_protocol import CLI_FAILURES
    from agent_comms.launch_root import LaunchRoot
    from .order import Commander

    try:
        # Every comms process selects its root once at startup, as the comms CLI does.
        LaunchRoot.startup(argv, lambda: (LaunchRoot.from_path(options.root) if options.root
                                          else ActiveRoute.published_launch()))
        context = Commander.read_root(resolve_comms_route(options.root).root)
        entry(argv[1:], context)
    except CLI_FAILURES as refused:
        # The comms CLI's one failure set: a refusal the command raises prints, never a traceback.
        sys.exit(f'Refused: {refused}')
