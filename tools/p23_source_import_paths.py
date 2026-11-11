"""Show native NRA import-graph paths behind the rejected Codex moves."""

import argparse
import json
from collections import deque
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from nominal_refactor_advisor.codemod_import_graph import SourceModuleImportGraph
from nominal_refactor_advisor.source_index import SourceFileDigest, SourceIndex


def import_path(graph: SourceModuleImportGraph, start: str, target: str) -> tuple[str, ...]:
    pending = deque([(start,)])
    visited = {start}
    while pending:
        path = pending.popleft()
        if path[-1] == target:
            return path
        for neighbor in sorted(graph.import_edges_by_module.get(path[-1], ())):
            if neighbor not in visited:
                visited.add(neighbor)
                pending.append((*path, neighbor))
    return ()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("src/agent_comms"))
    parser.add_argument("--origin", default="agent_comms.cli_bridges.codex_rollout")
    parser.add_argument("--target", default="agent_comms.importing")
    args = parser.parse_args()
    modules = parse_python_modules(args.root, parse_workers=1)
    graph = SourceModuleImportGraph(
        SourceIndex(files=tuple(SourceFileDigest.from_module(module) for module in modules)),
        {module.file_path: module.module for module in modules},
    )
    print(json.dumps({"origin": args.origin, "target": args.target, "paths": {
        neighbor: import_path(graph, neighbor, args.target)
        for neighbor in sorted(graph.import_edges_by_module[args.origin])
    }}, indent=2))


if __name__ == "__main__":
    main()
