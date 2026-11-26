"""OpenHCS agent communications — JSON CLI.

Commands print one JSON result, except watch, which streams messages as JSON
lines. Errors are one JSON line carrying a declared type and error text.
They exit 0 on success or 1 on failure. This is the adapter surface for the pi extension and other
process-based clients: they shell out and parse JSON, they do not own
orchestration semantics.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path

CLI_DESCRIPTION = __doc__


def _snake_command_name(class_name: str) -> str:
    stem = class_name[:-10] if class_name.endswith("CliCommand") and len(class_name) > 10 else class_name
    return re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()


def discover_cli_verbs() -> dict[str, str]:
    """Scan ``agent_comms/cli_verbs/*.py`` via AST for every concrete ``CliCommand`` verb
    and its help text, matching ``ModuleMembers.validate_module_name``."""
    package_root = Path(__file__).resolve().parent
    verbs_dir = package_root / "cli_verbs"
    support_dir = package_root / "cli_support"
    classes: dict[str, tuple[str, tuple[str, ...], str | None, bool, bool, str | None, bool]] = {}
    for is_verb_dir, directory in ((True, verbs_dir), (False, support_dir)):
        for path in sorted(directory.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                bases: list[str] = []
                is_abc = False
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        bases.append(base.id)
                        if base.id == "ABC":
                            is_abc = True
                    elif isinstance(base, ast.Attribute):
                        bases.append(base.attr)
                    elif isinstance(base, ast.Subscript):
                        if isinstance(base.value, ast.Name):
                            bases.append(base.value.id)
                        elif isinstance(base.value, ast.Attribute):
                            bases.append(base.value.attr)
                declared_name: str | None = None
                declared_none = False
                for kw in node.keywords:
                    if kw.arg == "declared_name":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                            declared_none = True
                        elif isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            declared_name = kw.value.value
                        else:
                            declared_name = path.stem.replace("_", "-")
                help_text: str | None = None
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if (
                                isinstance(target, ast.Name)
                                and isinstance(stmt.value, ast.Constant)
                                and isinstance(stmt.value.value, str)
                            ):
                                if target.id == "help":
                                    help_text = stmt.value.value
                                elif target.id == "declared_name":
                                    declared_name = stmt.value.value
                    elif (
                        isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    ):
                        if stmt.target.id == "help":
                            help_text = stmt.value.value
                        elif stmt.target.id == "declared_name":
                            declared_name = stmt.value.value
                classes[node.name] = (
                    path.stem,
                    tuple(bases),
                    declared_name,
                    declared_none,
                    is_abc,
                    help_text,
                    is_verb_dir,
                )

    def resolve_help(class_name: str) -> str:
        seen: set[str] = set()
        queue = [class_name]
        while queue:
            current = queue.pop(0)
            if current in seen or current not in classes:
                continue
            seen.add(current)
            _, bases, _, _, _, text, _ = classes[current]
            if text is not None:
                return text
            queue.extend(bases)
        return ""

    verbs: dict[str, str] = {}
    for class_name, (stem, _, declared_name, declared_none, is_abc, _, is_verb_dir) in classes.items():
        if not is_verb_dir or not class_name.endswith("CliCommand") or class_name == "CliCommand":
            continue
        if declared_none or is_abc:
            continue
        name = declared_name if declared_name is not None else _snake_command_name(class_name)
        if name.replace("-", "_") != stem:
            continue
        verbs[name] = resolve_help(class_name)
    return dict(sorted(verbs.items()))


def is_top_level_help(arguments: Sequence[str]) -> bool:
    idx = 0
    saw_help = False
    while idx < len(arguments):
        token = arguments[idx]
        if token in ("-h", "--help"):
            saw_help = True
            idx += 1
        elif token == "--root" and idx + 1 < len(arguments):
            idx += 2
        elif token.startswith("--root="):
            idx += 1
        else:
            return False
    return saw_help


def format_top_level_help() -> str:
    parser = argparse.ArgumentParser(prog="agent-comms", description=CLI_DESCRIPTION)
    parser.add_argument(
        "--root",
        help="Explicit Comms root directory (otherwise uses the published active route)",
    )
    subparsers = parser.add_subparsers(required=True)
    for name, help_text in discover_cli_verbs().items():
        subparsers.add_parser(name, help=help_text)
    return parser.format_help()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = argv if isinstance(argv, Sequence) else sys.argv[1:]
    if is_top_level_help(arguments):
        sys.stdout.write(format_top_level_help())
        return 0
    return import_module("agent_comms.cli_runtime").main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
