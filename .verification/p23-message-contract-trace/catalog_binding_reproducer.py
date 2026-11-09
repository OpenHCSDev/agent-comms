"""Small native NRA module-move reproducer; inputs remain virtual, no product writes."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot
from nominal_refactor_advisor.codemod_preflight import CodemodOperationPreflightError
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.module_node_index import ModuleNodeIndex

ARTIFACTS = Path(__file__).resolve().parent
VIRTUAL = ARTIFACTS / "virtual-inputs"
SOURCE = VIRTUAL / "pkg/relationships.py"
CONSUMER = VIRTUAL / "pkg/presentation.py"
PLAN = {"stages": [{"recipes": [{"recipe_id": "catalog-class-field-scope-reproducer", "operations": [
    {"operation": "extract_symbols_to_new_module", "file_path": str(SOURCE),
     "symbol_qualnames": ["RelationshipCatalogSource"],
     "destination_path": str(VIRTUAL / "pkg/catalog_source.py")}
]}]}]}


def main() -> None:
    outcomes = []
    for label, field in (("class_field", "    relationships: int\n"), ("no_field_control", "")):
        sources = {
            str(VIRTUAL / "pkg/__init__.py"): "",
            str(SOURCE): "class RelationshipCatalogSource:\n    catalog: object\n\nclass RelationshipStore:\n    pass\n",
            str(CONSUMER): "from pkg import relationships\n\nclass WireRevision:\n" + field
                          + "    path = lambda self: relationships.RelationshipStore\n",
        }
        snapshot = CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(VIRTUAL,))
        parsed = snapshot.parsed_module_for_source_path(str(CONSUMER))
        index = parsed.fact(ModuleNodeIndex)
        sites = []
        for site in index.binding_sites["relationships"]:
            scope = index.binding_scope(site, "relationships")
            sites.append({"node": ast.dump(site), "reported_scope": "module" if scope is None else type(scope).__name__})
        result = {"variant": label, "sources": sources, "plan": PLAN,
                  "reported_module_bindings": index.module_scope_bindings("relationships"),
                  "binding_sites": sites}
        try:
            simulation = CodemodPlanRoot.from_json_value(PLAN).as_sequence().simulate(snapshot)
        except CodemodOperationPreflightError as error:
            result.update({"is_clean": False, "error": str(error), "preflight": json_report_object(error.report)})
        else:
            result.update({"is_clean": simulation.is_clean, "stages": simulation.stage_count})
        outcomes.append(result)
        print(f"{label}: module bindings={result['reported_module_bindings']}; clean={result['is_clean']}", flush=True)
    (ARTIFACTS / "catalog-class-field-reproducer.json").write_text(json.dumps(outcomes, indent=2) + "\n")


if __name__ == "__main__":
    main()
