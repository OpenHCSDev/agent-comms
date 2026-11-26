"""`agent-comms-team TOOL ARGS...`: each module in this package that declares `main` is one tool."""
import argparse
import ast
import pkgutil
import sys
from importlib import import_module
from pathlib import Path

import agent_comms.team as package


def tool_name(module: str) -> str:
    """The tool a module of this package is: its name, with dashes."""
    return module.rsplit('.', 1)[1].replace('_', '-')


def _discover_tools() -> dict[str, tuple[str, str]]:
    """Find every tool module in ``agent_comms.team`` declaring top-level ``main`` without
    importing heavy runtime modules just to print usage."""
    discovered: dict[str, tuple[str, str]] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name == 'cli':
            continue
        source_path = Path(info.module_finder.path) / f'{info.name}.py'  # type: ignore[attr-defined]
        if not source_path.is_file():
            continue
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        if not any(isinstance(node, ast.FunctionDef) and node.name == 'main' for node in tree.body):
            continue
        doc = ast.get_docstring(tree) or ''
        headline = doc.splitlines()[0] if doc else ''
        module_name = f'{package.__name__}.{info.name}'
        discovered[tool_name(module_name)] = (module_name, headline)
    return discovered


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--root', type=Path, metavar='ROOT')
    options, argv = parser.parse_known_args(sys.argv[1:])
    tools = _discover_tools()
    if not argv or argv[0] not in tools:
        lines = [f'usage: agent-comms-team {{{",".join(sorted(tools))}}} ARGS...', '']
        lines += [f'  {name:<11} {tools[name][1]}' for name in sorted(tools)]
        asked = argv[:1] in (['-h'], ['--help'])
        print('\n'.join(lines), file=sys.stdout if asked else sys.stderr)
        sys.exit(0 if asked else 2)

    module_name, _ = tools[argv[0]]
    tool = import_module(module_name)
    runner = import_module('agent_comms.team.order')
    runner.run_team_cli(tool.main, argv, options.root)
