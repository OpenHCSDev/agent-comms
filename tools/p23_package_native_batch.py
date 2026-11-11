"""Use NRA's existing package recipe owner directly, without an unrelated detector scan."""

import argparse
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import CompactModuleIdentity, parse_python_module_roots
from nominal_refactor_advisor.codemod import (
    CodemodPlanDocument, CodemodPlanSequence, CodemodSourceSnapshot,
)
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.package_move import ClusterLayers, DestinationTable
from nominal_refactor_advisor.scan_cache import ScanCache


@ScanCache.scope()
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", type=Path)
    parser.add_argument("plan_out", type=Path)
    parser.add_argument("roots", type=Path, nargs="+")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--hold-module", action="append", default=[])
    args = parser.parse_args()
    modules = parse_python_module_roots(tuple(path.resolve() for path in args.roots))
    original = CodemodSourceSnapshot.from_modules(modules)
    table = DestinationTable.from_texts((args.table.read_text(),))
    destinations = table.destinations(tuple(
        CompactModuleIdentity(module.module_name, module.file_path) for module in modules
    ))
    if not destinations:
        print(json.dumps({"applied": False, "reason": "No declared flat module remains"}))
        return 0
    first = destinations[0]
    whole = ClusterLayers(original, first.cluster_paths, first.cluster_destination_paths,
                          first.cluster_merge_source_paths)
    mutual = whole.mutual_imports
    held = frozenset(path for pair in mutual for path in pair) | frozenset(
        path for path in first.cluster_paths if Path(path).stem in args.hold_module
    )
    paths = tuple(path for path in first.cluster_paths if path not in held)
    layers = ClusterLayers(original, paths, tuple(whole.destinations[path] for path in paths),
                           tuple(path for path in first.cluster_merge_source_paths if path in paths))
    batch = layers.batch_paths
    if not batch:
        print(json.dumps({"applied": False, "mutual_imports": mutual,
                          "refusals": layers.batch_refusals,
                          "reason": "No existing native relocation batch is executable"}, indent=2))
        return 1
    recipe = layers.batch_recipe()
    moving = CodemodPlanDocument(recipes=(recipe,))
    args.plan_out.write_text(json.dumps(json_report_object(moving), indent=2) + "\n")
    preflight = moving.preflight(original)
    args.plan_out.with_suffix(".inputs.json").write_text(json.dumps({
        "moved_modules": batch, "held_mutual_imports": mutual,
        "held_modules": tuple(sorted(held)), "held_refusals": layers.batch_refusals,
        "text_file_changes": {
            path: tuple(change.file_path for change in operation.text_file_changes(preflight.rewrite_snapshot))
            for path, operation in zip(batch, preflight.document.recipes[0].operations, strict=True)
        },
    }, indent=2) + "\n")
    moved = moving.as_sequence().simulate(original)
    if not moved.is_clean:
        print(json.dumps({"applied": False, **moved.execution_payload()}, indent=2))
        return 1
    after = moved.final_snapshot
    remaining = ClusterLayers(after, paths, tuple(whole.destinations[path] for path in paths),
                              tuple(path for path in first.cluster_merge_source_paths if path in paths))
    documents = [moving]
    if remaining.dissolving_paths:
        documents.append(CodemodPlanDocument(recipes=(remaining.dissolve_recipe(),)))
    sequence = CodemodPlanSequence.compose(documents)
    args.plan_out.write_text(json.dumps(json_report_object(sequence), indent=2) + "\n")
    result = sequence.simulate(original)
    applied = result.is_clean and args.apply
    if applied:
        result.apply()
    print(json.dumps({"applied": applied, "moved_modules": batch,
                      "held_mutual_imports": mutual, "held_refusals": layers.batch_refusals,
                      **result.execution_payload()}, indent=2))
    return 0 if result.is_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
