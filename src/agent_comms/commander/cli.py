"""`agent-comms-commander TOOL ARGS...`: each module in this package that declares `main` is one tool."""
import pkgutil
import sys
from importlib import import_module

import agent_comms.commander as package


def main() -> None:
    modules = (import_module(f'{package.__name__}.{m.name}') for m in pkgutil.iter_modules(package.__path__) if m.name != 'cli')
    tools = {module.__name__.rsplit('.', 1)[1].replace('_', '-'): module for module in modules if hasattr(module, 'main')}
    if len(sys.argv) < 2 or sys.argv[1] not in tools:
        sys.exit(f'usage: agent-comms-commander {{{",".join(sorted(tools))}}} ARGS...')
    tools[sys.argv[1]].main(sys.argv[2:])
