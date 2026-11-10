"""Remove rebased import copies using retained NRA relocation declarations.

Only delete an old import when the same binding already imports the moved
symbol from its current declared module. Missing bindings remain OPEN.
"""

import argparse
import ast
import importlib.util
import json
from collections import defaultdict
from pathlib import Path

from nominal_refactor_advisor.ast_tools import ModuleSyntaxIndex
from pydantic import JsonValue
from p23_rebase_imports import ModuleMoves, ROOT, git, module_name


def relocations(value: JsonValue):
    match value:
        case {"operation": "relocate_symbols_to_new_module", "file_path": str() as source,
              "destination_path": str() as destination, "symbol_qualnames": list() as names}:
            yield Path(source), Path(destination), names
        case dict():
            for child in value.values():
                yield from relocations(child)
        case list():
            for child in value:
                yield from relocations(child)


def absolute_import(node: ast.ImportFrom, filename: str) -> str:
    module = node.module or ""
    if not node.level:
        return module
    name = module_name(filename)
    package = name if filename.endswith("/__init__.py") else name.rpartition(".")[0]
    return importlib.util.resolve_name("." * node.level + module, package)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("recipes", type=Path, nargs="+")
    arguments = parser.parse_args()
    moves = ModuleMoves.at(arguments.target)
    owners = {}
    omitted = []
    for recipe in arguments.recipes:
        for source, destination, names in relocations(json.loads(recipe.read_text())):
            if not source.is_relative_to(ROOT) or not destination.is_relative_to(ROOT):
                continue
            source_name = module_name(str(source.relative_to(ROOT)))
            destination_name = module_name(str(destination.relative_to(ROOT)))
            source_name = moves.modules.get(source_name, source_name)
            destination_name = moves.modules.get(destination_name, destination_name)
            current = ROOT / "src" / Path(*destination_name.split("."))
            current = current.with_suffix(".py")
            if not current.is_file():
                omitted.append({"recipe": str(recipe), "destination": destination_name})
                continue
            tree = ast.parse(current.read_text(), filename=str(current))
            declared = {node.name for node in tree.body
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
            for name in names:
                if name in declared:
                    owners[source_name, name] = destination_name
    removals = defaultdict(set)
    opened = []
    for filename in git("ls-files", "src", "tests").splitlines():
        if not filename.endswith(".py"):
            continue
        tree = ast.parse((ROOT / filename).read_text(), filename=filename)
        syntax = ModuleSyntaxIndex.build(tree)
        imports = syntax.instances_of(ast.ImportFrom)
        bound = {(absolute_import(node, filename), alias.name, alias.asname)
                 for node in tree.body if isinstance(node, ast.ImportFrom)
                 for alias in node.names}
        for node in imports:
            module = absolute_import(node, filename)
            for alias in node.names:
                destination = owners.get((module, alias.name))
                if destination is None or destination == module:
                    continue
                if (destination, alias.name, alias.asname) in bound:
                    removals[filename, "." * node.level + (node.module or "")].add(alias.name)
                else:
                    opened.append({"path": filename, "line": node.lineno,
                                   "name": alias.name, "destination": destination})
    operations = [{"operation": "remove_import_names", "file_path": str(ROOT / filename),
                   "module_name": module, "import_names": sorted(names)}
                  for (filename, module), names in sorted(removals.items())]
    arguments.plan.write_text(json.dumps({"recipes": [{
        "recipe_id": "p23-delete-rebased-relocation-import-copies",
        "reason": "Retained NRA relocation declarations and current destination definitions identify these moved symbols. Delete only old imports whose same binding already has the current module import. Bodies and current bindings stay untouched. Missing replacement bindings and obsolete historical destinations remain OPEN; native imports and consumers remain required.",
        "operations": operations, "architecture_guards": [], "authority_claims": []
    }], "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"operations": len(operations), "open": opened,
                      "obsolete_destinations": omitted}, indent=2))


if __name__ == "__main__":
    main()
