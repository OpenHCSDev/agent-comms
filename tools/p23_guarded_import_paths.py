"""Use NRA's single parsed import graph to locate the remaining guarded edges."""

import argparse
import ast
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from nominal_refactor_advisor.codemod_import_graph import SourceModuleImportGraph
from nominal_refactor_advisor.source_index import SourceFileDigest, SourceIndex

from p23_source_import_paths import import_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="+")
    parser.add_argument("--root", type=Path, default=Path("src/agent_comms"))
    args = parser.parse_args()
    modules = parse_python_modules(args.root, parse_workers=1)
    by_name = {module.module_name: module for module in modules}
    graph = SourceModuleImportGraph(
        SourceIndex(files=tuple(SourceFileDigest.from_module(module) for module in modules)),
        {module.file_path: module.module for module in modules},
    )
    result = {}
    for name in args.modules:
        module = by_name[name]
        source_file = graph.unique_source_file_for_module_name(name)
        assert source_file is not None
        edges = []
        for conditional in module.syntax_index.instances_of(ast.If):
            match conditional.test:
                case ast.Name(id="TYPE_CHECKING"):
                    for statement in conditional.body:
                        for imported in graph.statement_edges(source_file, statement):
                            edges.append({"line": statement.lineno, "imported": imported,
                                          "return_path": import_path(graph, imported, name)})
        result[name] = edges
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
