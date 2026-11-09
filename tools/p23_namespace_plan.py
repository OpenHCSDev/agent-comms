"""Prepare NRA import patches from pinned upstream moves; never write source."""

import argparse
import ast
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import ModuleSyntaxIndex
from p23_rebase_imports import ModuleMoves, ROOT, git


def moved_literals(source: str, filename: str, moves: ModuleMoves) -> str:
    lines = source.splitlines(keepends=True)
    syntax = ModuleSyntaxIndex.build(ast.parse(source, filename=filename))
    for node in sorted(syntax.instances_of(ast.Constant),
                       key=lambda item: (item.lineno, item.col_offset), reverse=True):
        match node.value:
            case str() as value if value in moves.modules:
                assert node.end_lineno == node.lineno
                assert node.end_col_offset is not None
                line = lines[node.lineno - 1].encode()
                lines[node.lineno - 1] = (
                    line[:node.col_offset] + repr(moves.modules[value]).encode()
                    + line[node.end_col_offset:]
                ).decode()
    return "".join(lines)


def missing_imports(source: str, filename: str) -> list[dict[str, str | int]]:
    result = []
    syntax = ModuleSyntaxIndex.build(ast.parse(source, filename=filename))
    for node in syntax.instances_of(ast.ImportFrom):
        module = node.module or ""
        if not module.startswith("agent_comms."):
            continue
        path = ROOT / "src" / Path(*module.split("."))
        if not path.is_dir() and not path.with_suffix(".py").is_file():
            result.append({"path": filename, "line": node.lineno, "module": module})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--plan", required=True, type=Path)
    arguments = parser.parse_args()
    moves = ModuleMoves.at(arguments.target)
    paths = [path for path in git("diff", "--name-only", "--diff-filter=ACMR",
                                  arguments.target, "--", "src", "tests").splitlines()
             if path.endswith(".py")]
    operations = []
    missing = []
    for filename in paths:
        source = (ROOT / filename).read_text()
        after = moves.normalize(source, filename, changed_only=True)
        after = moved_literals(after, filename, moves)
        missing.extend(missing_imports(after, filename))
        if after == source:
            continue
        operations.append({
            "operation": "patch_target", "file_path": str(ROOT / filename),
            "replacements": [{"old_source": source, "new_source": after}],
        })
    plan = {"recipes": [{
        "recipe_id": "p23-close-pinned-namespace-moves",
        "reason": (
            "Pinned upstream Git moves determine module identity. Migrate P23 "
            "import consumers and exact module literals to those declarations, "
            "retaining bindings, comments and executable bodies. Ruff owns relative "
            "import conversion. This is namespace repair, not behavior proof. "
            "Unmapped imports stay in the report; cold imports, runtime hints "
            "and original native consumers remain required."
        ),
        "operations": operations, "architecture_guards": [], "authority_claims": [],
    }], "architecture_guards": []}
    arguments.plan.write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps({"target": arguments.target, "files": len(paths),
                      "patched": len(operations), "missing": missing}, indent=2))


if __name__ == "__main__":
    main()
